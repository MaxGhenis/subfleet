"""Revive defects fixed after the 2026-09-05 seat death (forensics 2026-09-06).

1. A revive must leave a LIVE session: the default host is a detached tmux
   session running interactive ``claude --resume``; the one-shot ``-p`` host
   is a fallback only, and a one-shot's exit after a turn that armed
   background tasks or left detached runs pending is classified as needing
   continuation — never as "completed".
2. The nudge and revive messages assert no cause: only the transcript
   classification, the limit-banner detector when it fired, and the launch
   subfleet itself made. Where the cause is unknown the text says so.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import cli, tickle
from test_notify import SESSION, _wait_lines, registry  # noqa: F401  (fixture reuse)
from test_tickle import TOOL_RESULT, TOOL_USE, _entry, _write

COLD = "cccccccc-dead-4000-8000-000000000001"


def _bg_bash(uuid: str, tool_id: str, *, age_s: float, description: str) -> dict:
    return _entry("assistant", [{"type": "tool_use", "id": tool_id, "name": "Bash",
                                 "input": {"command": "sleep 1140; echo due", "description": description,
                                           "run_in_background": True}}],
                  uuid=uuid, age_s=age_s, entrypoint="sdk-cli")


def _bg_result(uuid: str, tool_id: str, task_id: str, *, age_s: float) -> dict:
    return _entry("user", [{"type": "tool_result", "tool_use_id": tool_id,
                            "content": f"Command running in background with ID: {task_id}. Output is being "
                                       "written to: /private/tmp/x/tasks/{task_id}.output. You will be "
                                       "notified when it completes."}],
                  uuid=uuid, age_s=age_s, entrypoint="sdk-cli")


def forensic_transcript(*, age_s: float = 600, cwd: str = "/work/repo") -> list[dict]:
    """The 29c03102 tail as the one-shot revive left it at 23:06:32: the
    interrupted app-hosted turn, the app's resume stub, the task-notification
    for the three watchers the app killed, the revive prompt, a turn that
    arms four background watchers and dispatches a detached run, and a
    closing assistant text with stop_reason end_turn."""
    t = age_s
    rows = [
        _entry("user", "verify run 3", uuid="u0", age_s=t + 400, cwd=cwd, permissionMode="bypassPermissions"),
        _entry("assistant", TOOL_USE, uuid="a0", age_s=t + 390, entrypoint="claude-desktop"),
        _entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "Exit code 137"}],
               uuid="u1", age_s=t + 380, entrypoint="claude-desktop"),
        # the one-shot host's own start
        {"type": "user", "isMeta": True, "uuid": "stub-u", "entrypoint": "sdk-cli",
         "message": {"role": "user", "content": [{"type": "text", "text": "Continue from where you left off."}]}},
        {"type": "assistant", "uuid": "stub-a", "entrypoint": "sdk-cli",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "No response requested."}]}},
        _entry("user", "<task-notification>\n<task-id>bgewiby6o</task-id>\n<status>stopped</status>\n"
                       "<summary>3 background shell command task(s) from the previous session have no "
                       "completion record.</summary>\n</task-notification>",
               uuid="u2", age_s=t + 300, entrypoint="sdk-cli", promptSource="sdk"),
        _entry("user", tickle.message({"detail": "a tool result arrived but the model never continued"},
                                      revive={"host": "print", "lane": "lane@x", "model": "claude-fable-5-1"}),
               uuid="u3", age_s=t + 299, entrypoint="sdk-cli", promptSource="sdk"),
        _entry("assistant", [{"type": "text", "text": "Re-establishing state."}], uuid="a1", age_s=t + 280,
               entrypoint="sdk-cli"),
        _bg_bash("a2", "tu-1", age_s=t + 270, description="Re-arm the combined watcher"),
        _bg_result("u4", "tu-1", "bhit7b0nw", age_s=t + 269),
        _bg_bash("a3", "tu-2", age_s=t + 260, description="Re-arm the sweep timer for 23:21"),
        _bg_result("u5", "tu-2", "b3tlwal70", age_s=t + 259),
        _entry("assistant", [{"type": "tool_use", "id": "tu-3", "name": "Bash",
                              "input": {"command": "subfleet run --task build -p brief.md -o out.md"}}],
               uuid="a4", age_s=t + 200, entrypoint="sdk-cli"),
        _entry("user", [{"type": "tool_result", "tool_use_id": "tu-3",
                         "content": "subfleet run: dispatched run=20260905-230323-plan-a-a1-live-verify"}],
               uuid="u6", age_s=t + 199, entrypoint="sdk-cli"),
        _bg_bash("a5", "tu-4", age_s=t + 100, description="Watch the verification lane"),
        _bg_result("u7", "tu-4", "bkebmcge3", age_s=t + 99),
        _bg_bash("a6", "tu-5", age_s=t + 40, description="Watch ingestion run 4"),
        _bg_result("u8", "tu-5", "bnikmc7z3", age_s=t + 39),
        _entry("assistant", [{"type": "text", "text": "Privately, what I need next: the verification "
                                                       "lane's report and run 4's report."}],
               uuid="a7", age_s=t, entrypoint="sdk-cli"),
    ]
    rows[-1]["message"]["stop_reason"] = "end_turn"
    return rows


def _index(tmp_path, monkeypatch, cli_id: str, cwd: str = "/work/repo") -> None:
    store = tmp_path / "session-store"
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    directory = store / "acct" / "org"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"local_{cli_id}.json").write_text(json.dumps(
        {"cliSessionId": cli_id, "cwd": cwd, "permissionMode": "bypassPermissions"}))


# --------------------------------------------------------------------------
# 1. classification: a one-shot's exit is not "completed" work
# --------------------------------------------------------------------------

def test_last_turn_arms_reads_the_harness_result_text(tmp_path):
    path = _write(tmp_path / "t.jsonl", forensic_transcript())
    arms = tickle.last_turn_arms(path)
    assert [arm["id"] for arm in arms] == ["bhit7b0nw", "b3tlwal70", "bkebmcge3", "bnikmc7z3"]
    assert {arm["kind"] for arm in arms} == {"shell"}
    # the previous turn's watchers (already reported dead by the app) are not counted
    state = tickle.turn_state(path)
    assert state["state"] == "completed"
    assert [arm["id"] for arm in state["armed_tasks"]] == ["bhit7b0nw", "b3tlwal70", "bkebmcge3", "bnikmc7z3"]


@pytest.mark.parametrize(("result_text", "kind", "ident"), [
    ("Async agent launched successfully.\nagentId: ab6da25eb634f6552 (internal)", "agent", "ab6da25eb634f6552"),
    ("Monitor started (task bw714ljw3, timeout 180000ms). You will be notified.", "monitor", "bw714ljw3"),
    ("Workflow launched in background. Task ID: wit9peoco\nSummary: x", "workflow", "wit9peoco"),
    ("Next wakeup scheduled for 11:47:00 (in 1514s).", "wakeup", "11:47:00"),
])
def test_agents_monitors_workflows_and_wakeups_count_as_arms(tmp_path, result_text, kind, ident):
    rows = [
        _entry("user", "go", uuid="u1", age_s=700),
        _entry("assistant", [{"type": "tool_use", "id": "tu", "name": "X", "input": {}}], uuid="a1", age_s=690),
        _entry("user", [{"type": "tool_result", "tool_use_id": "tu", "content": result_text}], uuid="u2", age_s=680),
        _entry("assistant", [{"type": "text", "text": "Waiting."}], uuid="a2", age_s=600),
    ]
    arms = tickle.last_turn_arms(_write(tmp_path / "t.jsonl", rows))
    assert arms == [{"kind": kind, "id": ident, "tool_use_id": "tu"}]


def test_a_synchronous_turn_arms_nothing(tmp_path):
    rows = [
        _entry("user", "go", uuid="u1", age_s=700),
        _entry("assistant", TOOL_USE, uuid="a1", age_s=690),
        _entry("user", TOOL_RESULT, uuid="u2", age_s=680),
        _entry("assistant", [{"type": "tool_use", "id": "tu-a", "name": "Agent",
                              "input": {"prompt": "x", "run_in_background": False}}], uuid="a2", age_s=670),
        _entry("user", [{"type": "tool_result", "tool_use_id": "tu-a", "content": "the agent's answer"}],
               uuid="u3", age_s=660),
        _entry("assistant", [{"type": "text", "text": "All done."}], uuid="a3", age_s=600),
    ]
    state = tickle.turn_state(_write(tmp_path / "t.jsonl", rows))
    assert state["state"] == "completed" and state["armed_tasks"] == []
    assert tickle.continuation_needed(state, COLD) is None


def test_a_print_host_exit_after_arming_watchers_is_never_completed_work(registry, tmp_path, monkeypatch):
    """The 2026-09-05 gap, replayed: the tail the one-shot left classifies
    as needing continuation when the session has no process, and the sweep
    revives it (the next Continue) instead of parking it as completed."""
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    path = _write(projects / f"{COLD}.jsonl", forensic_transcript())
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    rows = tickle.cold_sessions()
    assert [row["session_id"] for row in rows] == [COLD]
    assert rows[0]["state"] == "needs-continuation"
    assert "4 background task(s) armed in its last turn" in rows[0]["detail"]
    assert "need a live process" in rows[0]["detail"]
    # ...and the same tail on a LIVE session is left alone by the hook path
    assert tickle.decide(SESSION, path, source="resume")["tickle"] is False
    _index(tmp_path, monkeypatch, COLD)
    launched = []
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@example.com", "tok"))
    monkeypatch.setattr(tickle, "revive_session",
                        lambda cli, cwd, token, *, bypass, model=None, popen=None, **kw:
                        launched.append((cli, kw.get("state", {}).get("state"), kw.get("email"))) or 4242)
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert results[COLD].get("revived") is True and results[COLD]["tail"] == "needs-continuation"
    assert launched == [(COLD, "needs-continuation", "lane@example.com")]
    # once per stuck point
    again = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert "already revived" in again[COLD]["skip"]


def test_pending_detached_runs_keep_a_completed_cold_tail_revivable(registry, tmp_path, monkeypatch):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    rows = [
        _entry("user", "dispatch and wait", uuid="u1", age_s=700),
        _entry("assistant", [{"type": "text", "text": "Dispatched; waiting for the notice."}], uuid="a1", age_s=600),
    ]
    _write(projects / f"{COLD}.jsonl", rows)
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    state_dir = Path(os.environ["SUBFLEET_STATE_DIR"])
    run_dir = state_dir / "runs" / "20260905-230323-plan-a-a1-live-verify"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "id": run_dir.name, "family": "claude", "model": "claude-opus-5", "lane": "lane@x",
        "started_at": "2026-09-05T23:03:23-04:00", "finished_at": None, "pid": os.getpid(),
        "caller": {"session_id": COLD},
    }))
    assert tickle.pending_runs(COLD) == [run_dir.name]
    rows = tickle.cold_sessions()
    assert [row["session_id"] for row in rows] == [COLD]
    assert rows[0]["state"] == "needs-continuation" and run_dir.name in rows[0]["detail"]
    # an orphaned run (runner pid gone) is not pending
    meta = json.loads((run_dir / "meta.json").read_text())
    meta["pid"] = 999999
    (run_dir / "meta.json").write_text(json.dumps(meta))
    assert tickle.pending_runs(COLD) == []
    assert tickle.cold_sessions() == []


def test_a_genuinely_finished_cold_session_stays_out(registry, tmp_path, monkeypatch):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    _write(projects / f"{COLD}.jsonl", [_entry("user", "x", uuid="u1", age_s=700),
                                        _entry("assistant", "All done.", uuid="a1", age_s=600)])
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    assert tickle.cold_sessions() == []


def test_revive_loop_guard_parks_a_session_relaunched_repeatedly(registry, tmp_path, monkeypatch):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    _write(projects / f"{COLD}.jsonl", [_entry("user", "x", uuid="u1", age_s=700),
                                        _entry("assistant", TOOL_USE, uuid="a1", age_s=600)])
    _index(tmp_path, monkeypatch, COLD)
    now = datetime.now(timezone.utc)
    tickle.save_record(COLD, {"history": [
        {"at": (now - timedelta(minutes=10 * i)).isoformat(), "revive": True, "pid": 100 + i, "uuid": f"k{i}"}
        for i in range(1, tickle.REVIVE_LOOP_MAX + 1)
    ]})
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: pytest.fail("a parked session must not be probed"))
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert "revive loop guard" in results[COLD]["skip"]
    # launches outside the window do not count
    tickle.save_record(COLD, {"history": [
        {"at": (now - timedelta(hours=3)).isoformat(), "revive": True, "pid": 1, "uuid": "old"}]})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@x", "tok"))
    monkeypatch.setattr(tickle, "revive_session", lambda *a, **kw: 4242)
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert results[COLD].get("revived") is True


# --------------------------------------------------------------------------
# 2. messages: nothing asserted that was not detected
# --------------------------------------------------------------------------

CAUSE_CLAIMS = ("usage limit or account switch", "account switch", "app relaunch", "fresh account",
                "fresh one now", "hit its usage limit")


def test_nudge_message_asserts_no_cause():
    state = {"state": "interrupted", "detail": "a tool result arrived but the model never continued"}
    text = tickle.message(state)
    assert text.startswith(tickle.MARKER)
    assert "a tool result arrived but the model never continued" in text
    assert tickle.CAUSE_UNKNOWN in text
    for claim in CAUSE_CLAIMS:
        assert claim not in text, claim
    assert "usage-limit banner" not in text  # the detector did not fire


def test_nudge_message_carries_the_limit_line_only_when_detected():
    state = {"state": "interrupted", "detail": "the model was cut off by a usage limit after its last text",
             "limit_banner": True}
    text = tickle.message(state)
    assert "Detected in the transcript" in text and "usage-limit banner" in text
    assert tickle.CAUSE_UNKNOWN in text
    for claim in CAUSE_CLAIMS:
        assert claim not in text, claim
    assert tickle.limit_line({"limit_banner": False}) == ""


def test_revive_message_names_only_the_launch_it_made():
    state = {"state": "needs-continuation",
             "detail": "last turn ended in assistant text, but 4 background task(s) armed in its last turn"}
    launch = {"host": tickle.REVIVE_HOST_TMUX, "lane": "max@example.com", "model": "claude-fable-5-1"}
    text = tickle.message(state, revive=launch)
    assert text.startswith(tickle.MARKER)
    assert "lane max@example.com" in text and "claude-fable-5-1" in text and "tmux host" in text
    assert tickle.CAUSE_UNKNOWN in text and "EXCEPTION" in text
    assert "died with it" in text and "re-arm" in text
    assert "one-shot" not in text
    for claim in CAUSE_CLAIMS:
        assert claim not in text, claim
    one_shot = tickle.message(state, revive={**launch, "host": tickle.REVIVE_HOST_PRINT})
    assert "one-shot" in one_shot and "subfleet re-issues a continue" in one_shot
    # a revive message in the transcript still reads as subfleet's own nudge
    assert tickle.turn_state(None)["state"] == "empty"


def test_module_has_no_fixed_revive_template():
    assert not hasattr(tickle, "REVIVE_MESSAGE")
    source = Path(tickle.__file__).read_text()
    assert "usage limit or account switch" not in source
    assert "you are on a fresh account now" not in source


# --------------------------------------------------------------------------
# 3. the persistent host
# --------------------------------------------------------------------------

class _Result:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def test_revive_session_prefers_a_tmux_host_and_keeps_the_token_out_of_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "/explicit/claude")
    monkeypatch.setenv("SUBFLEET_TMUX", "/fake/tmux")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-launching-session")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    calls = []

    def runner(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if "display-message" in cmd:
            return _Result(stdout="7777\n")
        return _Result()

    popen_calls = []
    pid = tickle.revive_session(COLD, "/work/repo", "oauth-token", bypass=True, model="claude-fable-5-1",
                                email="max@example.com", state={"detail": "d"}, runner=runner,
                                popen=lambda *a, **kw: popen_calls.append(a) or pytest.fail("no one-shot"))
    assert pid == 7777 and popen_calls == []
    new_session, kwargs = calls[0]
    # the dedicated server (no config file): Max's default server must never host these
    assert new_session[:9] == ["/fake/tmux", "-L", "subfleet", "-f", "/dev/null",
                               "new-session", "-d", "-s", tickle.tmux_session_name(COLD)]
    assert "-c" in new_session and new_session[new_session.index("-c") + 1] == "/work/repo"
    shell = new_session[-1]
    assert shell.startswith("exec ") and shell.split()[1].endswith("bin/subfleet-revive-host")
    assert f"--session {COLD}" in shell and "--lane max@example.com" in shell
    assert "--claude /explicit/claude" in shell and "--model claude-fable-5-1" in shell and "--bypass" in shell
    assert "oauth-token" not in " ".join(new_session)
    env = kwargs["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env  # the host must not inherit the launcher's identity
    assert calls[1][0][:7] == ["/fake/tmux", "-L", "subfleet", "-f", "/dev/null", "display-message", "-p"]
    pending = tickle.load_record(COLD)["revive_pending"]
    assert pending["host"] == "tmux" and pending["lane"] == "max@example.com"
    assert pending["tmux_session"] == tickle.tmux_session_name(COLD) and pending["pid"] == 7777


def test_revive_session_falls_back_to_the_one_shot_when_tmux_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "claude")
    monkeypatch.setenv("SUBFLEET_TMUX", "/fake/tmux")
    popen_calls = []

    class Proc:
        pid = 4242

    def popen(cmd, **kwargs):
        popen_calls.append((list(cmd), kwargs))
        return Proc()

    def failing_runner(cmd, **kwargs):
        return _Result(rc=1, stderr="no server running")

    pid = tickle.revive_session(COLD, "/work/repo", "oauth-token", bypass=True, model="claude-opus-5",
                                email="lane@x", state={"detail": "cut off"}, runner=failing_runner, popen=popen)
    assert pid == 4242 and len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd[:4] == ["claude", "-p", "--resume", COLD]
    assert cmd[4].startswith(tickle.MARKER) and "print host" in cmd[4] and "one-shot" in cmd[4]
    assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert "falling back to the one-shot host" in capsys.readouterr().err
    pending = tickle.load_record(COLD)["revive_pending"]
    assert pending["host"] == "print" and pending["fallback"]
    # no tmux at all → one-shot without an error
    monkeypatch.setenv("SUBFLEET_TMUX", "")
    monkeypatch.setattr(tickle.shutil, "which", lambda name: None)
    monkeypatch.setattr(tickle.os, "access", lambda path, mode: False)
    assert tickle.revive_session(COLD, "/work/repo", "tok", bypass=True, email="lane@x", popen=popen) == 4242
    # configured one-shot host
    monkeypatch.setenv("SUBFLEET_REVIVE_HOST", "print")
    assert tickle.revive_host() == tickle.REVIVE_HOST_PRINT


def test_tmux_launch_without_a_readable_pane_pid_is_torn_down(monkeypatch):
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", "claude")
    monkeypatch.setenv("SUBFLEET_TMUX", "/fake/tmux")
    calls = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        if "display-message" in cmd:
            return _Result(stdout="not a pid")
        return _Result()

    launch = tickle._launch_tmux_host(COLD, "/work", email="l@x", model=None, bypass=True, runner=runner)
    assert launch["ok"] is False and "pane pid" in launch["error"]
    assert calls[-1][0] == "/fake/tmux" and calls[-1][5:7] == ["kill-session", "-t"]


def test_census_counts_tmux_hosts_and_one_shots(monkeypatch):
    monkeypatch.setenv("SUBFLEET_TMUX", "/fake/tmux")
    hosted = "11111111-1111-4111-8111-111111111111"
    one_shot = "22222222-2222-4222-8222-222222222222"
    desktop = "33333333-3333-4333-8333-333333333333"
    ps = "\n".join([
        f"501 400 claude --resume {hosted} --model claude-fable-5-1 --dangerously-skip-permissions",
        f"601 1 claude -p --resume {one_shot} subfleet: this session restarted",
        f"701 58585 claude --resume={desktop} --output-format stream-json",
    ])

    def runner(cmd, **kwargs):
        if cmd[0] == "ps":
            return _Result(stdout=ps)
        if "list-sessions" in cmd:  # asked of the dedicated server and the default one
            return _Result(stdout=f"c\northestrator\n{tickle.tmux_session_name(hosted)}\nsubfleet-revive-bogus\n")
        return _Result(rc=1)

    assert tickle.tmux_revive_hosts(runner) == {hosted}
    assert tickle.live_revive_sessions(runner=runner) == {one_shot: [601], hosted: [501]}
    # no tmux server: only the one-shot
    monkeypatch.setenv("SUBFLEET_TMUX", "/nonexistent/tmux")

    def no_tmux(cmd, **kwargs):
        if cmd[0] == "ps":
            return _Result(stdout=ps)
        raise FileNotFoundError(cmd[0])

    assert tickle.live_revive_sessions(runner=no_tmux) == {one_shot: [601]}


def test_persistent_hosts_do_not_occupy_the_launch_cap(registry, tmp_path, monkeypatch):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    _write(projects / f"{COLD}.jsonl", [_entry("user", "x", uuid="u1", age_s=700),
                                        _entry("assistant", TOOL_USE, uuid="a1", age_s=600)])
    _index(tmp_path, monkeypatch, COLD)
    hosted = "44444444-4444-4444-8444-444444444444"
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {hosted: [501]})
    monkeypatch.setattr(tickle, "tmux_revive_hosts", lambda runner=None: {hosted})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@x", "tok"))
    monkeypatch.setattr(tickle, "revive_session", lambda *a, **kw: 4242)
    results = {r.get("session_id"): r for r in tickle.auto_revive(max_batch=1)}
    assert results[COLD].get("revived") is True
    # a one-shot in flight does
    monkeypatch.setattr(tickle, "tmux_revive_hosts", lambda runner=None: set())
    tickle.save_record(COLD, {})
    results = {r.get("session_id"): r for r in tickle.auto_revive(max_batch=1)}
    assert results[COLD]["skip"] == "batch cap reached"


def test_auto_revive_spawns_the_inbox_deliverer_for_a_tmux_host(registry, tmp_path, monkeypatch):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    transcript = _write(projects / f"{COLD}.jsonl", [_entry("user", "x", uuid="u1", age_s=700),
                                                     _entry("assistant", TOOL_USE, uuid="a1", age_s=600)])
    _index(tmp_path, monkeypatch, COLD)
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    monkeypatch.setattr(tickle, "probe_lane", lambda **kw: ("lane@x", "tok"))

    def fake_revive(cli, cwd, token, *, bypass, model=None, popen=None, email=None, state=None, **kw):
        tickle._note_launch(cli, {"host": "tmux", "lane": email, "model": model,
                                  "tmux_session": tickle.tmux_session_name(cli), "pid": 7777})
        return 7777

    monkeypatch.setattr(tickle, "revive_session", fake_revive)
    spawned = []
    monkeypatch.setattr(tickle, "spawn", lambda sid, path, **kw: spawned.append((sid, str(path), kw)) or 99)
    results = {r.get("session_id"): r for r in tickle.auto_revive()}
    assert results[COLD]["host"] == "tmux" and results[COLD]["tmux_session"] == tickle.tmux_session_name(COLD)
    assert spawned == [(COLD, str(transcript), {"delay_s": tickle.REVIVE_NUDGE_DELAY_S,
                                                 "await_inbox_s": tickle.REVIVE_NUDGE_AWAIT_INBOX_S})]
    record = tickle.load_record(COLD)
    assert record["revived"] == "a1" and record["history"][-1]["host"] == "tmux"
    assert record["revive_pending"]["host"] == "tmux"


def test_deliver_frames_the_pending_revive_and_clears_it(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    path = _write(projects / f"{SESSION}.jsonl", [
        _entry("user", "x", uuid="u1", permissionMode="bypassPermissions"),
        _entry("assistant", TOOL_USE, uuid="a1"),
    ])
    tickle.retire_session(SESSION, "keep me")  # an unrelated marker that must survive delivery
    record = tickle.load_record(SESSION)
    record.pop("retired")
    record["revived"] = "a1"
    tickle.save_record(SESSION, record)
    tickle._note_launch(SESSION, {"host": "tmux", "lane": "max@example.com", "model": "claude-fable-5-1",
                                  "tmux_session": tickle.tmux_session_name(SESSION), "pid": 7777})
    verdict = tickle.deliver(SESSION, path, delay_s=0, await_inbox_s=30, sleep=lambda s: None)
    assert verdict["delivered"] is True, verdict
    content = _wait_lines(registry["inbox"], 2)[1]["message"]["content"]
    assert "lane max@example.com" in content and "tmux host" in content and tickle.CAUSE_UNKNOWN in content
    for claim in CAUSE_CLAIMS:
        assert claim not in content, claim
    record = tickle.load_record(SESSION)
    assert "revive_pending" not in record and record["revive_delivered"]["host"] == "tmux"
    assert record["revived"] == "a1" and record["last_uuid"] == "a1"
    assert record["history"][-1]["revive"] == "tmux"


def test_deliver_nudges_a_completed_tail_only_for_a_pending_revive(registry, tmp_path):
    projects = registry["claude_dir"] / "projects" / "-Users-max"
    path = _write(projects / f"{SESSION}.jsonl", forensic_transcript())
    assert tickle.deliver(SESSION, path, delay_s=0)["delivered"] is False
    tickle._note_launch(SESSION, {"host": "tmux", "lane": "l@x", "model": "m", "detail": "4 tasks armed"})
    verdict = tickle.deliver(SESSION, path, delay_s=0)
    assert verdict["delivered"] is True and "revived by subfleet" in verdict["reason"]
    assert "4 tasks armed" in _wait_lines(registry["inbox"], 2)[1]["message"]["content"]
    # an expired marker no longer reframes anything
    stale = tickle.load_record(SESSION)
    stale["revive_pending"] = {"at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "host": "tmux"}
    tickle.save_record(SESSION, stale)
    assert tickle.revive_pending(stale) is None


def test_deliver_waits_for_the_inbox_and_gives_up_without_consuming_the_point(tmp_path, monkeypatch):
    projects = Path(os.environ["SUBFLEET_CLAUDE_DIR"]) / "projects" / "-Users-max"
    path = _write(projects / f"{COLD}.jsonl", [_entry("assistant", TOOL_USE, uuid="a1")])
    ticks = iter(range(0, 1000))
    slept = []
    verdict = tickle.deliver(COLD, path, await_inbox_s=5, clock=lambda: next(ticks), sleep=slept.append)
    assert verdict["delivered"] is False and "no live inbox" in verdict["reason"]
    assert slept and all(s == 1.0 for s in slept)
    assert tickle.load_record(COLD).get("last_uuid") is None
    # a failed push does not consume the interruption point either
    monkeypatch.setattr(tickle.notify, "push_to_session",
                        lambda *a, **kw: {"delivered": False, "reason": "no-inbox-socket"})
    verdict = tickle.deliver(COLD, path, delay_s=0)
    assert verdict["delivered"] is False
    assert tickle.load_record(COLD).get("last_uuid") is None


def test_spawn_and_worker_forward_await_inbox(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(tickle, "deliver", lambda sid, transcript, **kw: seen.update(kw) or {"delivered": True})
    assert cli.main(["_tickle", "--session", COLD, "--delay", "2", "--await-inbox", "120"]) == 0
    assert seen["await_inbox_s"] == 120.0 and seen["delay_s"] == 2.0
    fake = tmp_path / "fake-subfleet"
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$*" > "$SPAWN_LOG"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("SPAWN_LOG", str(tmp_path / "spawn.log"))
    tickle.spawn(COLD, tmp_path / "t.jsonl", delay_s=15, await_inbox_s=240, executable=str(fake))
    deadline = datetime.now() + timedelta(seconds=5)
    while datetime.now() < deadline and not (tmp_path / "spawn.log").exists():
        pass
    assert (tmp_path / "spawn.log").read_text().split() == [
        "_tickle", "--session", COLD, "--delay", "15", "--await-inbox", "240", "--transcript", str(tmp_path / "t.jsonl")]


def test_revive_host_script_fetches_the_token_and_execs_interactive_resume(tmp_path, monkeypatch):
    script = Path(tickle.revive_host_script())
    assert script.exists() and script.stat().st_mode & stat.S_IXUSR
    secret = tmp_path / "agent-secret"
    secret.write_text('#!/bin/bash\n[ "$1" = get ] && [ "$2" = "claude-quota-lane@x" ] && echo lane-token-123\n')
    secret.chmod(0o755)
    claude = tmp_path / "claude"
    claude.write_text('#!/bin/bash\nprintf "%s\\n" "$*" > "$OUT"\nenv >> "$OUT"\n')
    claude.chmod(0o755)
    out = tmp_path / "out.txt"
    log = tmp_path / "host.log"
    env = {**os.environ, "SUBFLEET_AGENT_SECRET": str(secret), "OUT": str(out),
           "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "launcher", "ANTHROPIC_API_KEY": "leak"}
    subprocess.run([str(script), "--session", COLD, "--lane", "lane@x", "--claude", str(claude),
                    "--model", "claude-fable-5-1", "--bypass", "--log", str(log)],
                   env=env, check=True, timeout=30)
    lines = out.read_text().splitlines()
    assert lines[0] == f"--resume {COLD} --model claude-fable-5-1 --dangerously-skip-permissions"
    assert "CLAUDE_CODE_OAUTH_TOKEN=lane-token-123" in lines
    assert not any(line.startswith(("CLAUDECODE=", "CLAUDE_CODE_SESSION_ID=", "ANTHROPIC_API_KEY=")) for line in lines)
    assert f"SUBFLEET_REVIVE_HOST_SESSION={COLD}" in lines
    assert "exec" in log.read_text() and "lane-token-123" not in log.read_text()
    # no token → the host reports and exits nonzero instead of launching
    secret.write_text("#!/bin/bash\nexit 1\n")
    proc = subprocess.run([str(script), "--session", COLD, "--lane", "lane@x", "--claude", str(claude),
                           "--log", str(log)], env={**env, "OUT": str(tmp_path / "never.txt")},
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 78 and "no token for lane" in proc.stderr
    assert not (tmp_path / "never.txt").exists()
