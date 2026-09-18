"""Revive into a persistent host; a one-shot's exit is never "completed" while
work is pending; nudge text carries only detected facts (2026-09-06 fixes
after the 2026-09-05 seat-death forensics)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from subfleet import cli, lanes, liveness, tickle
from subfleet.util import iso, now_local
from test_notify import SESSION, FakeInbox, _wait_lines, registry  # noqa: F401  (fixture reuse)
from test_tickle import STUB_USER, TOOL_USE, _entry, _stub_assistant, _write

HOST_SCRIPT = Path(__file__).parent.parent / "bin" / "subfleet-revive-host"


def _bg_use(uuid: str, tool_id: str, age_s: float, name: str = "Bash", **params) -> dict:
    return _entry("assistant", [{"type": "tool_use", "id": tool_id, "name": name,
                                 "input": {"run_in_background": True, **params}}], uuid=uuid, age_s=age_s)


def _result(uuid: str, tool_id: str, text: str, age_s: float) -> dict:
    return _entry("user", [{"type": "tool_result", "tool_use_id": tool_id, "content": text}], uuid=uuid, age_s=age_s)


def _use(uuid: str, tool_id: str, name: str, age_s: float, **params) -> dict:
    return _entry("assistant", [{"type": "tool_use", "id": tool_id, "name": name, "input": params}],
                  uuid=uuid, age_s=age_s)


BG_TEXT = ("Command running in background with ID: {}. Output is being written to: /tmp/x/tasks/{}.output. "
           "You will be notified when it completes.")
AGENT_TEXT = ("Async agent launched successfully. (This tool result is internal metadata)\n"
              "agentId: ab6da25eb634f6552 (internal ID - do not mention to user.)\nThe agent is working in the background.")
MONITOR_TEXT = "Monitor started (task bw714ljw3, timeout 180000ms). You will be notified on each event."
WAKEUP_TEXT = "Next wakeup scheduled for 11:47:00 (in 1514s). Nothing more to do this turn."
WORKFLOW_TEXT = "Workflow launched in background. Task ID: wit9peoco\nSummary: re-derive"


# --------------------------------------------------------------------------
# Arms of the last turn, and what a completed tail means when nothing is alive
# --------------------------------------------------------------------------

def test_last_turn_arms_reads_the_harness_result_text(tmp_path):
    rows = [
        # an EARLIER turn that armed a watcher: outside the last turn, not counted
        _entry("user", "watch the lane", uuid="u0", age_s=3000),
        _bg_use("a0", "t0", 2990), _result("r0", "t0", BG_TEXT.format("old1", "old1"), 2985),
        _entry("assistant", [{"type": "text", "text": "armed"}], uuid="a0t", age_s=2980),
        # the last turn
        _entry("user", "continue", uuid="u1", age_s=900),
        _bg_use("a1", "t1", 890), _result("r1", "t1", BG_TEXT.format("bhit7b0nw", "bhit7b0nw"), 889),
        _use("a2", "t2", "Agent", 880, prompt="do x"), _result("r2", "t2", AGENT_TEXT, 879),
        _use("a3", "t3", "Monitor", 870, command="until ..."), _result("r3", "t3", MONITOR_TEXT, 869),
        _use("a4", "t4", "ScheduleWakeup", 860, delaySeconds=1500), _result("r4", "t4", WAKEUP_TEXT, 859),
        _use("a5", "t5", "Workflow", 850, script="..."), _result("r5", "t5", WORKFLOW_TEXT, 849),
        _use("a6", "t6", "Bash", 840, command="ls"), _result("r6", "t6", "ok", 839),  # synchronous: not an arm
        _use("a7", "t7", "ScheduleWakeup", 830, stop=True), _result("r7", "t7", "Loop stopped.", 829),
        _use("a8", "t8", "Agent", 820, run_in_background=False), _result("r8", "t8", "the agent's answer", 819),
        _entry("user", "subagent chatter", uuid="s1", age_s=815, isSidechain=True),
        _entry("assistant", [{"type": "text", "text": "Waiting on the watchers."}], uuid="a9", age_s=810),
    ]
    path = _write(tmp_path / "t.jsonl", rows)
    arms = tickle.last_turn_arms(path)
    assert [(arm["kind"], arm["id"]) for arm in arms] == [
        ("shell", "bhit7b0nw"), ("agent", "ab6da25eb634f6552"), ("monitor", "bw714ljw3"),
        ("wakeup", "11:47:00"), ("workflow", "wit9peoco"),
    ]
    state = tickle.turn_state(path)
    assert state["state"] == "completed" and len(state["armed_tasks"]) == 5
    # a tool_use with no result at all (process died mid-call) is still an arm
    dead = rows[:-1] + [_bg_use("a10", "t10", 805)]
    assert tickle.turn_state(_write(tmp_path / "dead.jsonl", dead))["state"] == "interrupted"
    assert tickle.last_turn_arms(tmp_path / "dead.jsonl")[-1] == {
        "kind": "tool_use", "id": "t10", "tool": "Bash", "unanswered": True}
    assert tickle.last_turn_arms(None) == [] and tickle.last_turn_arms(tmp_path / "nope.jsonl") == []


def _forensic_replay(projects: Path, cold_id: str, *, arms: int = 2) -> Path:
    """The 29c03102 sequence of 2026-09-05/06: app kill mid-tool (exit 137) →
    subfleet's one-shot revive turn: resume stub, the app's orphan
    notification, the revive prompt, watchers re-armed, a closing text."""
    launch = {"host": tickle.REVIVE_HOST_PRINT, "lane": "max@example.com", "model": "claude-fable-5-1"}
    rows = [
        _entry("user", "verify run 3", uuid="u1", age_s=1500, cwd="/work/repo"),
        _entry("assistant", TOOL_USE, uuid="a1", age_s=1450),
        _entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "Exit code 137"}], uuid="u2", age_s=1440),
        {**STUB_USER, "uuid": "stub-u9"}, _stub_assistant("stub-a9"),
        _entry("user", "<task-notification>\n<task-id>bgewiby6o</task-id>\n<status>stopped</status>\n</task-notification>",
               uuid="u3", age_s=620, promptSource="sdk", entrypoint="sdk-cli"),
        _entry("user", tickle.message({"detail": "a tool result arrived but the model never continued"}, revive=launch),
               uuid="u4", age_s=619, promptSource="sdk", entrypoint="sdk-cli"),
        _entry("assistant", [{"type": "text", "text": "Re-establishing state."}], uuid="a2", age_s=610, entrypoint="sdk-cli"),
    ]
    for index in range(arms):
        rows += [_bg_use(f"a{10 + index}", f"bg{index}", 600 - index, description=f"watcher {index}"),
                 _result(f"r{10 + index}", f"bg{index}", BG_TEXT.format(f"task{index}", f"task{index}"), 599 - index)]
    rows += [
        _use("a20", "sync", "Bash", 520, command="subfleet runs --mine"), _result("r20", "sync", "23:06 ...", 519),
        _entry("assistant", [{"type": "text", "text": "Privately, what I need next: the verification lane's report."}],
               uuid="a21", age_s=500, entrypoint="sdk-cli"),
    ]
    return _write(projects / f"{cold_id}.jsonl", rows)


def test_one_shot_exit_with_armed_watchers_needs_continuation_never_completed(registry, tmp_path, monkeypatch):
    """A revived session's transcript tail is never classified "completed"
    solely because a -p process exited after a turn that armed background
    tasks: the cold sweep lists it as needing continuation and revives it
    once more at that point. A live session with the same tail is left alone
    (its own process receives the notifications)."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    cold_id = "cccccccc-9999-4000-8000-000000000001"
    path = _forensic_replay(projects, cold_id)
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    state = tickle.turn_state(path)
    assert state["state"] == "completed" and [arm["id"] for arm in state["armed_tasks"]] == ["task0", "task1"]
    continuation = tickle.continuation_needed(state, cold_id)
    assert continuation and continuation["runs"] == [] and len(continuation["armed"]) == 2
    assert "2 background task(s) armed in its last turn (shell:task0, shell:task1)" in continuation["detail"]

    rows = tickle.cold_sessions()
    assert [row["session_id"] for row in rows] == [cold_id]
    assert rows[0]["state"] == "needs-continuation" and "need a live process" in rows[0]["detail"]

    # the live-session path (SessionStart hook) does not nudge a completed tail
    assert tickle.decide(cold_id, path, source="resume")["tickle"] is False

    # the sweep revives it (and remembers the point), then leaves it alone
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    (store / "acct" / "org").mkdir(parents=True)
    (store / "acct" / "org" / f"local_{cold_id}.json").write_text(json.dumps(
        {"cliSessionId": cold_id, "cwd": "/work/repo", "permissionMode": "bypassPermissions"}))
    launched = []
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@example.com", "tok"))
    monkeypatch.setattr(tickle, "revive_session",
                        lambda cli_id, cwd, token, *, bypass, model=None, popen=None, **kw:
                        launched.append((cli_id, kw.get("email"), (kw.get("state") or {}).get("state"))) or 4242)
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert results[cold_id]["revived"] is True and results[cold_id]["tail"] == "needs-continuation"
    assert launched == [(cold_id, "lane@example.com", "needs-continuation")]
    again = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert again[cold_id]["skip"] == "already revived at this point"

    # the same tail WITHOUT arms is a finished session: not cold, not revived
    done_id = "cccccccc-9999-4000-8000-000000000002"
    _forensic_replay(projects, done_id, arms=0)
    assert done_id not in {row["session_id"] for row in tickle.cold_sessions()}


def test_pending_detached_runs_keep_a_completed_tail_cold(registry, tmp_path, monkeypatch):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    cold_id = "cccccccc-9999-4000-8000-000000000003"
    _write(projects / f"{cold_id}.jsonl", [_entry("user", "dispatch it", uuid="u1", age_s=900),
                                           _entry("assistant", [{"type": "text", "text": "Dispatched; waiting."}],
                                                  uuid="a1", age_s=600)])
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    runs = Path(os.environ["SUBFLEET_STATE_DIR"]) / "runs"

    def ledger_row(run_id: str, **meta) -> None:
        (runs / run_id).mkdir(parents=True, exist_ok=True)
        (runs / run_id / "meta.json").write_text(json.dumps({
            "id": run_id, "family": "claude", "model": "claude-opus-5", "lane": "l@x",
            "caller": {"session_id": cold_id}, "pid": os.getpid(), "finished_at": None, **meta}))

    assert tickle.cold_sessions() == []
    ledger_row("20260905-230323-plan-a-a1-live-verify")
    assert tickle.pending_runs(cold_id) == ["20260905-230323-plan-a-a1-live-verify"]
    rows = tickle.cold_sessions()
    assert rows and rows[0]["state"] == "needs-continuation"
    assert "1 detached run(s) still pending (20260905-230323-plan-a-a1-live-verify)" in rows[0]["detail"]
    # finished → nothing pending; orphaned (runner pid gone) → nothing pending
    ledger_row("20260905-230323-plan-a-a1-live-verify", finished_at=iso(now_local()), rc=0)
    assert tickle.cold_sessions() == []
    ledger_row("20260905-230519-plan-a-live-ingest-r4", pid=999999)
    assert tickle.pending_runs(cold_id) == [] and tickle.cold_sessions() == []


# --------------------------------------------------------------------------
# Messages: detected facts only
# --------------------------------------------------------------------------

GUESSES = ("account switch", "app relaunch", "usage limit or", "fresh account", "fresh one")


def test_nudge_text_asserts_no_cause_it_did_not_detect(tmp_path):
    interrupted = {"state": "interrupted", "detail": "a tool call never got its result (Bash)", "limit_banner": False}
    text = tickle.message(interrupted)
    assert text.startswith(tickle.MARKER) and tickle.CAUSE_UNKNOWN in text
    assert "a tool call never got its result (Bash)" in text
    assert "Detected in the transcript" not in text
    assert not any(guess in text for guess in GUESSES)
    # the limit line appears only when the detector fired, and still names no cause
    limited = {**interrupted, "limit_banner": True}
    text = tickle.message(limited)
    assert "Detected in the transcript" in text and "usage-limit banner" in text
    assert tickle.CAUSE_UNKNOWN in text and not any(guess in text for guess in GUESSES)
    assert tickle.limit_line(interrupted) == "" and tickle.limit_line(limited)


def test_revive_text_names_only_what_subfleet_did(tmp_path):
    state = {"state": "needs-continuation", "detail": "2 background task(s) armed", "limit_banner": False}
    tmux = tickle.message(state, revive={"lane": "max@example.com", "model": "claude-fable-5-1", "host": "tmux"})
    assert "lane max@example.com" in tmux and "model claude-fable-5-1" in tmux and "tmux host" in tmux
    assert tickle.CAUSE_UNKNOWN in tmux and "EXCEPTION" in tmux and "re-arm what you still need" in tmux
    assert "one-shot" not in tmux and not any(guess in tmux for guess in GUESSES)
    print_host = tickle.message(state, revive={"lane": "max@x", "model": "m", "host": tickle.REVIVE_HOST_PRINT})
    assert "one-shot" in print_host and "exits when this turn ends" in print_host
    # both start with the marker the classifier recognises, so a delivered
    # nudge that got no answer reads "tickled", never "an unanswered prompt"
    for body in (tmux, print_host):
        path = _write(tmp_path / "n.jsonl", [_entry("assistant", "x", uuid="a0"), _entry("user", body, uuid="u1")])
        assert tickle.turn_state(path)["state"] == "tickled"


# --------------------------------------------------------------------------
# The persistent host
# --------------------------------------------------------------------------

def _fake_tmux(tmp_path: Path, *, new_session_rc: int = 0, pane_pid: str = "4321") -> tuple[Path, Path]:
    """A tmux stand-in that logs every call (with the server selector) and
    answers from the environment: FAKE_TMUX_SESSIONS (list-sessions lines),
    FAKE_TMUX_PANE (capture-pane text)."""
    log = tmp_path / "tmux.log"
    script = tmp_path / "fake-tmux"
    script.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "ARGS: $*" >> "{log}"\n'
        f'printf "%s\\n" "TOKEN=${{CLAUDE_CODE_OAUTH_TOKEN:-none}} SESSION=${{CLAUDE_CODE_SESSION_ID:-none}}" >> "{log}"\n'
        'while [ "$1" = -L ] || [ "$1" = -f ]; do shift 2; done\n'
        'case "$1" in\n'
        f"  new-session) exit {new_session_rc} ;;\n"
        f'  display-message) echo "{pane_pid}" ;;\n'
        '  list-sessions) printf "%s\\n" "c" ${FAKE_TMUX_SESSIONS:-subfleet-revive-11111111-1111-4111-8111-111111111111} ;;\n'
        '  capture-pane) printf "%s\\n" "${FAKE_TMUX_PANE:-}" ;;\n'
        '  kill-session) exit 0 ;;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def test_revive_session_prefers_a_tmux_host_and_keeps_the_token_out_of_argv(tmp_path, monkeypatch):
    fake, log = _fake_tmux(tmp_path)
    monkeypatch.setenv("SUBFLEET_TMUX", str(fake))
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "/opt/claude")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "launcher-session")
    cwd = str(tmp_path)  # subprocess.run needs a real cwd, even for a fake tmux
    pid = tickle.revive_session(SESSION, cwd, "oauth-token", bypass=True, model="claude-fable-5-1",
                                email="max@lane.ai", state={"detail": "cut off"})
    assert pid == 4321
    text = log.read_text()
    first = text.splitlines()[0]
    assert first.startswith(f"ARGS: -L subfleet -f /dev/null new-session -d -s subfleet-revive-{SESSION} -c {cwd}")
    assert f"subfleet-revive-host --session {SESSION} --lane max@lane.ai --claude /opt/claude" in first
    assert "--model claude-fable-5-1 --bypass" in first
    assert "oauth-token" not in text and "must-not-leak" not in text
    assert "TOKEN=none SESSION=none" in text, "no token or launcher identity reaches tmux"
    assert "ARGS: -L subfleet -f /dev/null display-message -p -t subfleet-revive-" in text
    record = tickle.load_record(SESSION)
    pending = record["revive_pending"]
    assert pending["host"] == "tmux" and pending["lane"] == "max@lane.ai" and pending["pid"] == 4321
    assert pending["server"] == "subfleet"
    assert pending["tmux_session"] == f"subfleet-revive-{SESSION}" and pending["detail"] == "cut off"
    assert tickle.revive_pending(record) == pending
    stale = {**record, "revive_pending": {**pending, "at": iso(now_local() - timedelta(hours=1))}}
    assert tickle.revive_pending(stale) is None


@pytest.mark.parametrize("why", ["tmux-fails", "no-tmux", "no-email", "configured-print"])
def test_revive_session_falls_back_to_the_one_shot(tmp_path, monkeypatch, capsys, why):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "claude")
    email = "max@lane.ai"
    if why == "tmux-fails":
        fake, _log = _fake_tmux(tmp_path, new_session_rc=1)
        monkeypatch.setenv("SUBFLEET_TMUX", str(fake))
    elif why == "no-tmux":
        monkeypatch.setenv("SUBFLEET_TMUX", str(tmp_path / "missing-tmux"))
        monkeypatch.setattr(tickle, "tmux_bin", lambda env=None: None)
    elif why == "no-email":
        email = None
    else:
        monkeypatch.setenv("SUBFLEET_REVIVE_HOST", "print")
    calls = []

    class Proc:
        pid = 777

    pid = tickle.revive_session(SESSION, "/work/repo", "oauth-token", bypass=True, model="claude-opus-5",
                                email=email, state={"detail": "cut off"},
                                popen=lambda cmd, **kw: calls.append((list(cmd), kw)) or Proc())
    assert pid == 777 and len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[:4] == ["claude", "-p", "--resume", SESSION] and cmd[-3:] == ["--model", "claude-opus-5", "--dangerously-skip-permissions"]
    assert "one-shot" in cmd[4] and tickle.CAUSE_UNKNOWN in cmd[4] and "cut off" in cmd[4]
    assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    pending = tickle.load_record(SESSION)["revive_pending"]
    assert pending["host"] == "print" and pending["pid"] == 777
    if why == "configured-print":
        assert pending.get("fallback") is None and "falling back" not in capsys.readouterr().err
    else:
        assert pending["fallback"], why
        assert "falling back to the one-shot host" in capsys.readouterr().err


def test_live_census_includes_tmux_hosts(monkeypatch):
    hosted = "11111111-1111-4111-8111-111111111111"
    one_shot = "22222222-2222-4222-8222-222222222222"
    desktop = "33333333-3333-4333-8333-333333333333"
    monkeypatch.setenv("SUBFLEET_TMUX", "/fake/tmux")
    listing = {"rc": 0}

    class Result:
        def __init__(self, stdout, rc=0):
            self.stdout, self.returncode = stdout, rc

    servers_asked = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ps":
            return Result("\n".join([
                f"555 300 claude --resume {hosted} --model claude-fable-5-1 --dangerously-skip-permissions",
                f"101 1 claude -p --resume {one_shot} subfleet: this session restarted",
                f"202 77 claude --resume={desktop} --output-format stream-json",
            ]))
        assert cmd[0] == "/fake/tmux" and "list-sessions" in cmd
        servers_asked.append(tuple(cmd[:cmd.index("list-sessions")]))
        return Result("c\nsubfleet-revive-" + hosted + "\nsubfleet-revive-not-a-uuid\n", listing["rc"])

    assert tickle.tmux_revive_hosts(fake_run) == {hosted}
    # both the dedicated server and the default one are asked (hosts launched
    # before the dedicated server existed live on the default one)
    assert servers_asked == [("/fake/tmux", "-L", "subfleet", "-f", "/dev/null"), ("/fake/tmux",)]
    assert tickle.live_revive_sessions(runner=fake_run) == {one_shot: [101], hosted: [555]}
    listing["rc"] = 1  # no tmux server: only one-shots are counted
    assert tickle.live_revive_sessions(runner=fake_run) == {one_shot: [101]}
    monkeypatch.setattr(tickle, "tmux_bin", lambda env=None: None)
    assert tickle.tmux_revive_hosts(fake_run) == set()


def test_auto_revive_cap_ignores_persistent_hosts_and_arms_the_nudger(tmp_path, monkeypatch):
    from test_tickle import _prepare_cold_revive_candidate

    ready = "aaaaaaaa-0000-4000-8000-000000000011"
    _prepare_cold_revive_candidate(tmp_path, monkeypatch, ready)
    hosted = {"aaaaaaaa-0000-4000-8000-0000000000a1": [1], "aaaaaaaa-0000-4000-8000-0000000000a2": [2]}
    monkeypatch.setattr(tickle, "cold_sessions", lambda: [{"session_id": ready, "age_s": 600, "detail": "ready"}])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: dict(hosted))
    monkeypatch.setattr(tickle, "tmux_revive_hosts", lambda runner=None: set(hosted))
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@x", "tok"))
    spawned = []
    monkeypatch.setattr(tickle, "spawn", lambda sid, transcript, **kw: spawned.append((sid, kw)) or 99)

    def fake_launch(cli_id, cwd, token, *, bypass, model=None, **kw):
        tickle._note_launch(cli_id, {"host": "tmux", "lane": kw.get("email"), "model": model,
                                     "tmux_session": tickle.tmux_session_name(cli_id), "pid": 4321})
        return 4321

    monkeypatch.setattr(tickle, "revive_session", fake_launch)
    results = {r["session_id"]: r for r in tickle.auto_revive(max_batch=1)}
    assert results[ready]["revived"] is True and results[ready]["host"] == "tmux"
    assert results[ready]["tmux_session"] == tickle.tmux_session_name(ready)
    assert spawned == [(ready, {"delay_s": tickle.REVIVE_NUDGE_DELAY_S,
                                "await_inbox_s": tickle.REVIVE_NUDGE_AWAIT_INBOX_S})]
    history = tickle.load_record(ready)["history"]
    assert history[-1]["revive"] is True and history[-1]["host"] == "tmux"
    # one-shot hosts still in flight DO occupy the cap
    monkeypatch.setattr(tickle, "tmux_revive_hosts", lambda runner=None: set())
    tickle.save_record(ready, {})
    assert tickle.auto_revive(max_batch=1) == [{"session_id": ready, "skip": "batch cap reached"}]


def test_revive_loop_guard_parks_a_flapping_session(tmp_path, monkeypatch):
    from test_tickle import _prepare_cold_revive_candidate

    flapping = "aaaaaaaa-0000-4000-8000-000000000012"
    _prepare_cold_revive_candidate(tmp_path, monkeypatch, flapping)
    monkeypatch.setattr(tickle, "cold_sessions", lambda: [{"session_id": flapping, "age_s": 600, "detail": "x"}])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@x", "tok"))
    monkeypatch.setattr(tickle, "revive_session", lambda *a, **kw: 4242)
    recent = [{"at": iso(now_local() - timedelta(minutes=10 * i)), "revive": True, "pid": i, "uuid": f"k{i}"}
              for i in range(1, tickle.REVIVE_LOOP_MAX + 1)]
    old = [{"at": iso(now_local() - timedelta(hours=5)), "revive": True, "pid": 9, "uuid": "k9"}]
    tickle.save_record(flapping, {"history": recent[:-1] + old})
    assert tickle.auto_revive()[0]["revived"] is True
    tickle.save_record(flapping, {"history": recent + old})
    outcome = tickle.auto_revive()[0]
    assert "revive loop guard" in outcome["skip"] and "parked for the liveness alert" in outcome["skip"]
    assert len(tickle.recent_revives(tickle.load_record(flapping))) == tickle.REVIVE_LOOP_MAX


def test_deliver_frames_the_pending_revive_and_keeps_the_record(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    path = _forensic_replay(projects, SESSION)
    tickle.save_record(SESSION, {"revived": "a21", "retired": None,
                                 "revive_pending": {"at": iso(now_local()), "host": "tmux", "lane": "max@lane.ai",
                                                    "model": "claude-fable-5-1", "tmux_session": "subfleet-revive-x",
                                                    "detail": "2 background task(s) armed"}})
    verdict = tickle.deliver(SESSION, path, delay_s=0)
    assert verdict["delivered"] is True, verdict
    assert verdict["reason"].startswith("revived by subfleet: 2 background task(s) armed")
    content = _wait_lines(registry["inbox"], 2)[1]["message"]["content"]
    assert "lane max@lane.ai" in content and "tmux host" in content and "EXCEPTION" in content
    assert tickle.CAUSE_UNKNOWN in content and not any(guess in content for guess in GUESSES)
    record = tickle.load_record(SESSION)
    assert record["revived"] == "a21", "deliver must not drop the launcher's markers"
    assert "revive_pending" not in record and record["revive_delivered"]["host"] == "tmux"
    # the stub sits mid-transcript here, so the point is the final text itself
    assert record["last_uuid"] == "a21" and record["history"][-1]["revive"] == "tmux"
    # the launcher's backstop deliverer has nothing left to do: the pending
    # marker is consumed and a completed tail is not nudged on its own
    again = tickle.deliver(SESSION, path, delay_s=0)
    assert again["delivered"] is False and again["reason"].startswith("completed:")
    assert len(registry["inbox"].lines) == 2


def test_deliver_waits_for_an_inbox_and_does_not_consume_the_point_without_one(tmp_path):
    projects = Path(os.environ["SUBFLEET_CLAUDE_DIR"]) / "projects" / "-Users-max"
    path = _write(projects / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1")])
    ticks = iter(range(0, 100))
    slept = []
    verdict = tickle.deliver(SESSION, path, await_inbox_s=3, sleep=slept.append, clock=lambda: next(ticks))
    assert verdict["delivered"] is False and "no live inbox" in verdict["reason"]
    assert slept and all(step == 1.0 for step in slept)
    record = tickle.load_record(SESSION)
    assert record.get("last_uuid") is None and record["history"][-1]["skip"].startswith("no live inbox")


def test_tickle_worker_forwards_await_inbox(monkeypatch):
    seen = {}
    monkeypatch.setattr(tickle, "deliver", lambda sid, transcript, **kw: seen.update(kw) or {"delivered": True})
    assert cli.main(["_tickle", "--session", SESSION, "--delay", "2", "--await-inbox", "30"]) == 0
    assert seen["await_inbox_s"] == 30.0 and seen["delay_s"] == 2.0
    assert tickle.spawn.__kwdefaults__["await_inbox_s"] == 0.0  # keyword-only, off unless asked


def test_spawn_passes_await_inbox_to_the_worker(tmp_path):
    fake = tmp_path / "fake-subfleet"
    log = tmp_path / "spawn.log"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" > "{log}"\n')
    fake.chmod(0o755)
    assert tickle.spawn(SESSION, tmp_path / "t.jsonl", delay_s=15, await_inbox_s=240, executable=str(fake))
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        import time
        time.sleep(0.05)
    assert log.read_text().strip() == f"_tickle --session {SESSION} --delay 15 --await-inbox 240 --transcript {tmp_path / 't.jsonl'}"


def test_revive_cli_prints_the_host(monkeypatch, capsys):
    monkeypatch.setattr(tickle, "auto_revive", lambda **kwargs: [{
        "session_id": SESSION, "revived": True, "pid": 4321, "host": "tmux",
        "tmux_session": f"subfleet-revive-{SESSION}", "lane": "lane@x", "model": "claude-fable-5-1", "detail": "cut off",
    }])
    assert cli.main(["revive"]) == 0
    out = capsys.readouterr().out
    assert f"host=tmux tmux=subfleet-revive-{SESSION}" in out and "model=claude-fable-5-1" in out


# --------------------------------------------------------------------------
# The pane command
# --------------------------------------------------------------------------

def test_revive_host_script_fetches_the_token_and_execs_interactive_claude(tmp_path):
    log = tmp_path / "claude.log"
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "ARGS: $*" "TOKEN=${{CLAUDE_CODE_OAUTH_TOKEN:-none}}" '
        f'"CLAUDECODE=${{CLAUDECODE:-unset}} SID=${{CLAUDE_CODE_SESSION_ID:-unset}}" '
        f'"API=${{ANTHROPIC_API_KEY:-unset}} HOST=${{SUBFLEET_REVIVE_HOST_SESSION:-unset}}" > "{log}"\n'
    )
    fake_claude.chmod(0o755)
    secret = tmp_path / "fake-agent-secret"
    secret.write_text('#!/bin/bash\n[ "$1" = get ] && [ "$2" = "claude-quota-max@lane.ai" ] && echo tok-123 && exit 0\nexit 1\n')
    secret.chmod(0o755)
    env = {**os.environ, "SUBFLEET_AGENT_SECRET": str(secret), "CLAUDECODE": "1",
           "CLAUDE_CODE_SESSION_ID": "launcher", "ANTHROPIC_API_KEY": "leak", "SUBFLEET_REVIVE_HOST_LINGER_S": "0"}
    proc = subprocess.run([str(HOST_SCRIPT), "--session", SESSION, "--lane", "max@lane.ai", "--claude", str(fake_claude),
                           "--model", "claude-fable-5-1", "--bypass", "--log", str(tmp_path / "host.log")],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    lines = log.read_text().splitlines()
    assert lines[0] == f"ARGS: --resume {SESSION} --model claude-fable-5-1 --dangerously-skip-permissions"
    assert lines[1] == "TOKEN=tok-123"
    assert lines[2] == "CLAUDECODE=unset SID=unset" and lines[3] == f"API=unset HOST={SESSION}"
    assert "exec" in (tmp_path / "host.log").read_text() and "tok-123" not in (tmp_path / "host.log").read_text()
    # no token → the host exits 78 without exec'ing claude
    log.unlink()
    proc = subprocess.run([str(HOST_SCRIPT), "--session", SESSION, "--lane", "nobody@x", "--claude", str(fake_claude)],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 78 and not log.exists() and "no token for lane nobody@x" in proc.stderr
    assert subprocess.run([str(HOST_SCRIPT)], capture_output=True, text=True, env=env).returncode == 2


# --------------------------------------------------------------------------
# The dedicated tmux server, stalled hosts, and the lane heuristic
# --------------------------------------------------------------------------

def test_tmux_servers_and_attach_hint(monkeypatch):
    monkeypatch.setenv("SUBFLEET_TMUX", "/fake/tmux")
    assert tickle.tmux_socket() == "subfleet"
    assert tickle._tmux_base() == ["/fake/tmux", "-L", "subfleet", "-f", "/dev/null"]
    assert tickle._tmux_servers() == [["/fake/tmux", "-L", "subfleet", "-f", "/dev/null"], ["/fake/tmux"]]
    assert tickle.tmux_attach_hint(SESSION) == f"tmux -L subfleet attach -t subfleet-revive-{SESSION}"
    monkeypatch.setenv("SUBFLEET_TMUX_SOCKET", "default")
    assert tickle.tmux_socket() == "" and tickle._tmux_servers() == [["/fake/tmux"]]
    assert tickle.tmux_attach_hint(SESSION) == f"tmux attach -t subfleet-revive-{SESSION}"
    monkeypatch.setattr(tickle, "tmux_bin", lambda env=None: None)
    assert tickle._tmux_base() is None and tickle._tmux_servers() == []


def test_launcher_raises_the_open_files_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(tickle.resource, "getrlimit", lambda kind: (256, 10240))
    monkeypatch.setattr(tickle.resource, "setrlimit", lambda kind, limits: seen.update({"limits": limits}))
    tickle._raise_nofile()
    assert seen["limits"] == (tickle.NOFILE_TARGET, 10240)
    seen.clear()
    monkeypatch.setattr(tickle.resource, "getrlimit", lambda kind: (256, 1024))
    tickle._raise_nofile()
    assert seen["limits"] == (1024, 1024), "never above the hard limit"
    seen.clear()
    monkeypatch.setattr(tickle.resource, "getrlimit", lambda kind: (65536, tickle.resource.RLIM_INFINITY))
    tickle._raise_nofile()
    assert seen == {}, "already high enough"


def test_stalled_tmux_host_is_reaped_and_the_point_reopened(tmp_path, monkeypatch, capsys):
    fake, log = _fake_tmux(tmp_path)
    monkeypatch.setenv("SUBFLEET_TMUX", str(fake))
    stalled = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("FAKE_TMUX_SESSIONS", f"subfleet-revive-{stalled}")
    monkeypatch.setenv("FAKE_TMUX_PANE", "You've hit your limit\nWhat do you want to do?\n"
                                          "1. Stop and wait for limit to reset\n3. Switch to usage credits")
    tickle.save_record(stalled, {"revived": "a9", "revive_pending": {"at": iso(now_local()), "host": "tmux"},
                                 "retired": None, "history": []})
    tickle.atomic_write_json(tickle._probe_cache_path(), {"models": {"claude-fable-5-1": {"email": "x", "at": 1}}})
    found = tickle.stalled_revive_hosts()
    assert found[stalled]["marker"] == "Stop and wait for limit to reset"
    assert found[stalled]["server"] == [str(fake), "-L", "subfleet", "-f", "/dev/null"]
    # dry run: reported, nothing touched
    assert tickle.reap_stalled_hosts(dry_run=True) == [{"session_id": stalled, "tmux_session": f"subfleet-revive-{stalled}",
                                                        "marker": "Stop and wait for limit to reset", "reaped": False}]
    assert "kill-session" not in log.read_text() and tickle.load_record(stalled)["revived"] == "a9"
    reaped = tickle.reap_stalled_hosts()
    assert reaped[0]["reaped"] is True
    assert f"ARGS: -L subfleet -f /dev/null kill-session -t subfleet-revive-{stalled}" in log.read_text()
    record = tickle.load_record(stalled)
    assert "revived" not in record and "revive_pending" not in record and record["retired"] is None
    assert record["history"][-1]["host_stalled"] == "Stop and wait for limit to reset" and record["history"][-1]["killed"] is True
    assert not tickle._probe_cache_path().exists(), "the cached lane is the one that hit its limit"
    # a healthy pane is not stalled
    monkeypatch.setenv("FAKE_TMUX_PANE", "❯ working on the verification report")
    assert tickle.stalled_revive_hosts() == {} and tickle.reap_stalled_hosts() == []
    # the revive pass reaps first and the CLI reports it
    monkeypatch.setenv("FAKE_TMUX_PANE", "2. Wait here, then continue automatically at Sep 12 at 10am")
    monkeypatch.setattr(tickle, "cold_sessions", lambda: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    results = tickle.auto_revive()
    assert results == [{"session_id": stalled, "stalled_host": "Wait here, then continue automatically",
                        "tmux_session": f"subfleet-revive-{stalled}", "reaped": True}]
    monkeypatch.setattr(tickle, "auto_revive", lambda **kw: results)
    assert cli.main(["revive"]) == 0
    assert "reaped stalled tmux host" in capsys.readouterr().out


def test_desktop_app_prompts_tagged_sdk_are_not_lanes(tmp_path, monkeypatch):
    """2026-09-07: Claude Code 2.1.263 tags the desktop app's typed prompts
    promptSource sdk (172 of the 400 newest transcripts); the heuristic must
    read the entrypoint too, or every fresh desktop session is a lane —
    refused a nudge, never listed cold, never revived (this session's own
    revive nudge was refused that way on 2026-09-06 08:15)."""
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    projects = Path(os.environ["SUBFLEET_CLAUDE_DIR"]) / "projects" / "-Users-max"
    shapes = {
        "desktop": {"entrypoint": "claude-desktop", "promptSource": "sdk"},
        "terminal": {"entrypoint": "cli", "promptSource": "sdk"},
        "untagged": {"promptSource": "sdk"},
        "typed": {"entrypoint": "cli", "promptSource": "typed"},
        "lane": {"entrypoint": "sdk-cli", "promptSource": "sdk"},
    }
    verdicts = {}
    for name, tags in shapes.items():
        sid = f"dddddddd-0000-4000-8000-{abs(hash(name)) % 10**12:012d}"
        path = _write(projects / f"{sid}.jsonl", [_entry("user", "Fix three defects", uuid="u1", age_s=900, **tags),
                                                   _entry("assistant", TOOL_USE, uuid="a1", age_s=600)])
        verdicts[name] = (lanes.headless_transcript(path), lanes.is_lane_session(sid, path, set()), sid)
    assert {name: v[0] for name, v in verdicts.items()} == {
        "desktop": False, "terminal": False, "untagged": False, "typed": False, "lane": True}
    assert {name: v[1] for name, v in verdicts.items()} == {name: v[0] for name, v in verdicts.items()}
    # a launched lane is still a lane by id, whatever its transcript says
    assert lanes.is_lane_session(verdicts["desktop"][2], None, {verdicts["desktop"][2]}) is True
    # and the cold sweep now sees the interrupted desktop session
    cold = {row["session_id"] for row in tickle.cold_sessions()}
    assert verdicts["desktop"][2] in cold and verdicts["terminal"][2] in cold and verdicts["lane"][2] not in cold


def test_liveness_treats_a_stalled_host_as_dead(tmp_path, monkeypatch):
    from test_liveness import _dead_session

    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    dead = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    _dead_session(claude_dir, dead, idle_min=30)
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {dead: [4321]})
    monkeypatch.setattr(tickle, "stalled_revive_hosts", lambda runner=None: {})
    assert liveness.roster()["rows"] == [], "a working host is a live session"
    monkeypatch.setattr(tickle, "stalled_revive_hosts",
                        lambda runner=None: {dead: {"session": f"subfleet-revive-{dead}", "server": [], "marker": "Switch to usage credits"}})
    [row] = liveness.roster()["rows"]
    assert row["host_stalled"] == "Switch to usage credits"
    text = liveness.format_alert(row)
    assert "stalled on a dialog (Switch to usage credits)" in text
    assert f"tmux -L subfleet attach -t subfleet-revive-{dead}" in text
