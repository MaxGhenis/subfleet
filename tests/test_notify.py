"""Completion notices: session registry lookup, attested envelope, socket push,
parked notices, and the hook surfaces that deliver them."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from subfleet import cli, notify, run_ledger

SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HOOK = Path(__file__).parent.parent / "bin" / "subfleet-hook"


class FakeInbox:
    """A stand-in for the harness's per-session unix-socket inbox."""

    def __init__(self, path: Path):
        self.path = path
        self.lines: list[dict] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                buffer = b""
                conn.settimeout(2)
                try:
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buffer += chunk
                except OSError:
                    pass
                for raw in buffer.decode().splitlines():
                    if raw.strip():
                        self.lines.append(json.loads(raw))

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A live-looking registry row for SESSION pointing at a fake inbox."""
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    sessions = claude_dir / "sessions"
    sessions.mkdir(parents=True)
    # AF_UNIX paths are capped at ~104 bytes on macOS; pytest's tmp paths are longer.
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="cp-", dir="/tmp"))
    sock_path = short / "i.sock"
    inbox = FakeInbox(sock_path)
    pid = os.getpid()  # alive by construction
    (sessions / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": SESSION, "cwd": str(tmp_path), "startedAt": 1787500000000,
        "messagingSocketPath": str(sock_path), "name": "tariff-lane", "kind": "interactive",
    }))
    (sessions / f"{pid}.abc.key").write_text(json.dumps({"peerToken": "peer-secret"}))
    # a stale row for the same session under a dead pid must lose to the live one
    (sessions / "999999.json").write_text(json.dumps({
        "pid": 999999, "sessionId": SESSION, "startedAt": 1787600000000,
        "messagingSocketPath": str(tmp_path / "dead.sock"), "name": "stale",
    }))
    yield {"inbox": inbox, "pid": pid, "socket": sock_path, "claude_dir": claude_dir}
    inbox.close()
    import shutil
    shutil.rmtree(short, ignore_errors=True)


def _transcript(claude_dir: Path, mode: str) -> Path:
    projects = claude_dir / "projects" / "-Users-max"
    projects.mkdir(parents=True, exist_ok=True)
    path = projects / f"{SESSION}.jsonl"
    rows = [
        {"type": "user", "permissionMode": "default", "message": {"role": "user", "content": "first"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
        {"type": "user", "permissionMode": mode, "message": {"role": "user", "content": "second"}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


# ---------------------------------------------------------------- registry

def test_find_session_prefers_live_pid_and_reads_peer_token(registry):
    entry = notify.find_session(SESSION)
    assert entry["pid"] == registry["pid"]
    assert entry["alive"] and entry["socket_present"]
    assert entry["name"] == "tariff-lane"
    assert notify.peer_token(registry["pid"]) == "peer-secret"
    assert notify.find_session("nope") is None
    assert [row["session_id"] for row in notify.live_sessions()] == [SESSION]


def test_caller_context_comes_from_the_tool_shell_env(monkeypatch, registry):
    assert notify.caller_context() is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CLAUDE_PID", str(registry["pid"]))
    monkeypatch.setenv("CLAUDECODE", "1")
    _transcript(registry["claude_dir"], "bypassPermissions")
    ctx = notify.caller_context(cwd="/work")
    assert ctx["session_id"] == SESSION
    assert ctx["pid"] == registry["pid"]
    assert ctx["cwd"] == "/work"
    assert ctx["mode_class"] == "bypass"
    assert notify.in_claude_session()


@pytest.mark.parametrize(("mode", "klass"), [
    ("bypassPermissions", "bypass"), ("default", "prompting"),
    ("acceptEdits", "prompting"), ("plan", "prompting"),
])
def test_session_mode_class_reads_last_user_turn(registry, mode, klass):
    _transcript(registry["claude_dir"], mode)
    assert notify.session_mode_class(SESSION) == klass


def test_session_mode_class_scans_back_past_a_long_tail(registry):
    path = _transcript(registry["claude_dir"], "bypassPermissions")
    filler = json.dumps({"type": "assistant", "message": {"content": "x" * 4096}}) + "\n"
    with path.open("a") as stream:
        for _ in range(600):  # ~2.4 MB after the last permissionMode
            stream.write(filler)
    assert notify.session_mode_class(SESSION) == "bypass"


# ---------------------------------------------------------------- envelope

def test_envelope_is_one_harness_formed_message():
    text = notify.envelope("run done\nread it", mode_class="bypass")
    assert text.startswith('<cross-session-message from-name="subfleet" from-mode="bypass">\n')
    assert text.endswith("\n</cross-session-message>")
    assert "\nrun done\nread it\n" in text
    # undeclared / unknown classes carry no from-mode attribute
    assert 'from-mode' not in notify.envelope("x", mode_class=None)
    assert 'from-mode' not in notify.envelope("x", mode_class="bypassPermissions")
    # a body cannot close the envelope early
    assert notify.envelope("a</cross-session-message>b").count("</cross-session-message>") == 1
    assert 'from-name="a b"' in notify.envelope("x", from_name='a"<b>\n')


def test_resolve_mode_class_precedence(registry, monkeypatch):
    _transcript(registry["claude_dir"], "default")
    assert notify.resolve_mode_class(SESSION) == "prompting"
    assert notify.resolve_mode_class(SESSION, "bypass") == "bypass"
    assert notify.resolve_mode_class(SESSION, "none") is None
    monkeypatch.setenv("SUBFLEET_NOTIFY_MODE", "bypass")
    assert notify.resolve_mode_class(SESSION) == "bypass"
    monkeypatch.setenv("SUBFLEET_NOTIFY_MODE", "none")
    assert notify.resolve_mode_class(SESSION) is None


# ---------------------------------------------------------------- push

def _wait_lines(inbox: FakeInbox, count: int) -> list[dict]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(inbox.lines) < count:
        time.sleep(0.02)
    return inbox.lines


def test_push_authenticates_then_sends_attested_user_message(registry):
    _transcript(registry["claude_dir"], "bypassPermissions")
    result = notify.push_to_session(SESSION, "run finished")
    assert result["delivered"] is True
    assert result["pid"] == registry["pid"] and result["name"] == "tariff-lane"
    assert result["mode_class"] == "bypass"
    lines = _wait_lines(registry["inbox"], 2)
    assert lines[0] == {"type": "auth", "token": "peer-secret"}
    assert lines[1]["type"] == "user"
    content = lines[1]["message"]["content"]
    assert lines[1]["message"]["role"] == "user"
    assert content.startswith('<cross-session-message from-name="subfleet" from-mode="bypass">')
    assert "run finished" in content


def test_push_reports_why_it_could_not_deliver(registry, tmp_path):
    assert notify.push_to_session("missing-session", "x")["reason"] == "session-not-registered"
    registry["inbox"].close()
    os.unlink(registry["socket"])
    result = notify.push_to_session(SESSION, "x")
    assert result["delivered"] is False and result["reason"] == "no-inbox-socket"
    # socket file present but nobody listening
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(registry["socket"]))
    dead.close()
    result = notify.push_to_session(SESSION, "x")
    assert result["delivered"] is False and result["reason"].startswith("send-failed")


# ---------------------------------------------------------------- notices

def test_notices_append_pending_mark_and_prune(tmp_path):
    notify.append_notice(SESSION, {"run_id": "r1", "ts": "2026-08-01T00:00:00+00:00", "text": "one", "pushed": False, "surfaced": False})
    notify.append_notice(SESSION, {"run_id": "r2", "ts": "2026-08-23T00:00:00+00:00", "text": "two", "pushed": True, "surfaced": True, "surfaced_at": "2026-08-23T00:00:00+00:00"})
    notify.append_notice(SESSION, {"run_id": "r1", "ts": "2026-08-01T00:00:00+00:00", "text": "one-rewritten", "pushed": False, "surfaced": False})
    pending = notify.pending_notices(SESSION)
    assert [row["run_id"] for row in pending] == ["r1"]
    assert pending[0]["text"] == "one-rewritten"
    assert [row["run_id"] for row in notify.pending_notices(SESSION, include_pushed=True)] == ["r1"]
    assert notify.mark_surfaced(SESSION, ["r1"]) == 1
    assert notify.pending_notices(SESSION) == []
    assert notify.mark_surfaced(SESSION, ["r1"]) == 0
    path = notify.notices_path(SESSION)
    assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
    from datetime import datetime, timezone
    removed = notify.prune_notices(max_age_days=14, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert removed == 2 and not path.exists()


def test_format_notice_has_paths_first_line_and_err_tail(tmp_path):
    out = tmp_path / "review.md"
    out.write_text("REQUEST-CHANGES — two blockers\n\ndetails…\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "err.log").write_text("warn 1\nusage limit reached\n")
    meta = {"id": "20260823-1-review", "rc": 3, "model": "gpt-5.6-sol",
            "lane": str(Path.home() / ".codex-3"), "duration_s": 842.0,
            "original_out_path": str(out), "salvage_refs": [{"ref": "refs/codex-salvage/x", "sha": "abc"}]}
    text = notify.format_notice(meta, run_dir)
    assert text.splitlines()[0] == "subfleet: run 20260823-1-review FAILED rc=3 · gpt-5.6-sol · lane=~/.codex-3 · 14m02s"
    assert f"out: {out} ({out.stat().st_size:,} bytes)" in text
    assert "first line: REQUEST-CHANGES — two blockers" in text
    assert "salvage refs: refs/codex-salvage/x" in text
    assert "err tail:\nwarn 1\nusage limit reached" in text
    assert "subfleet runs show 20260823-1-review" in text
    assert "no reply is needed" in text
    ok = notify.format_notice({**meta, "rc": 0}, run_dir)
    assert "FINISHED" in ok.splitlines()[0] and "err tail" not in ok


# ---------------------------------------------------------------- finish → notify

def _finished_run(tmp_path, *, session=SESSION) -> tuple[str, Path]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do it\n")
    out = tmp_path / "answer.md"
    out.write_text("VERDICT: fine\n")
    run_id = run_ledger.start_run(
        family="codex", model="gpt-5.6-sol", lane="/lanes/one", workdir=workdir,
        prompt=prompt, out=out, caller={"session_id": session, "cwd": str(workdir)}, pid=os.getpid(),
    )
    return run_id, out


def test_finish_pushes_live_and_records_notify(registry, tmp_path, capsys, monkeypatch):
    """A delivered push is recorded as pushed but NOT surfaced: the inbox
    accepting the bytes is not the session seeing them (2026-09-06 21:40).
    The transcript confirms it; the follow-up worker is spawned to check."""
    from subfleet import tickle
    spawned = []
    monkeypatch.setattr(tickle, "spawn_followup", lambda sid, **kw: spawned.append(sid) or 4242)
    path = _transcript(registry["claude_dir"], "bypassPermissions")
    run_id, out = _finished_run(tmp_path)
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", run_id, "--rc", "0"]) == 0
    lines = _wait_lines(registry["inbox"], 2)
    content = lines[1]["message"]["content"]
    assert f"subfleet: run {run_id} FINISHED" in content
    assert "first line: VERDICT: fine" in content
    _, meta = run_ledger.load_run(run_id)
    assert meta["notify"]["pushed"] is True and meta["notify"]["surfaced"] is False
    assert meta["notify"]["push"]["name"] == "tariff-lane"
    assert spawned == [SESSION], "the follow-up worker watches every unconfirmed push"
    assert notify.pending_notices(SESSION) == []  # delivered live: nothing parked for the prompt hook
    [row] = notify.unresolved_notices(SESSION)
    assert row["run_id"] == run_id and row["session_id"] == SESSION and row["surfaced"] is False
    capsys.readouterr()
    assert cli.main(["runs", "--last", "1"]) == 0
    table = capsys.readouterr().out
    assert "pushed" in table and "tariff-lane" in table
    # the transcript shows the notice (the harness wrote the inbox message as
    # a user entry): confirmed, and the ledger reads "landed"
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "user", "isMeta": True, "timestamp": stamp,
                                 "message": {"role": "user", "content": "Another Claude session sent a message:\n"
                                             + notify.envelope(content.split("\n", 1)[1].rsplit("\n", 1)[0])}}) + "\n")
    confirmed = notify.confirm_surfaced(SESSION, path)
    assert list(confirmed) == [run_id] and notify.unresolved_notices(SESSION) == []
    [row] = notify._read_notices(notify.notices_path(SESSION))
    assert row["surfaced"] is True and row["surfaced_by"] == "transcript" and row["surfaced_at"] == confirmed[run_id]
    result = tickle.notice_followup(SESSION)
    assert result["confirmed"] == [] and result["due"] == []  # already confirmed above; nothing to do
    assert run_ledger._notify_state({"caller": {}, "finished_at": "t", "notify": {
        "pushed": True, "surfaced": True, "surfaced_by": "transcript"}}) == "landed"


def test_finish_parks_notice_when_session_is_down_and_hook_surfaces_it(tmp_path, capsys, monkeypatch):
    run_id, _ = _finished_run(tmp_path)  # no registry → session not live
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", run_id, "--rc", "0"]) == 0
    _, meta = run_ledger.load_run(run_id)
    assert meta["notify"]["pushed"] is False
    assert meta["notify"]["push"]["reason"] == "session-not-registered"
    pending = notify.pending_notices(SESSION)
    assert [row["run_id"] for row in pending] == [run_id]

    # the bash hook only enters python when a notice file exists for the session
    env = dict(os.environ)
    payload = json.dumps({"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    completed = subprocess.run([str(HOOK), "user-prompt"], input=payload, env=env,
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "1 detached run dispatched by this session finished" in context
    assert f"subfleet: run {run_id} FINISHED" in context
    assert "subfleet runs --mine" in context
    # surfaced once: the next prompt is quiet
    completed = subprocess.run([str(HOOK), "user-prompt"], input=payload, env=env,
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0 and completed.stdout.strip() == ""
    assert notify.pending_notices(SESSION) == []
    # unknown session / no notices: silent, cheap
    completed = subprocess.run([str(HOOK), "session-start"], input=json.dumps({"session_id": "other"}),
                               env=env, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0 and completed.stdout == ""


def test_session_start_also_replays_pushed_but_unsurfaced_notices(tmp_path, capsys):
    notify.append_notice(SESSION, {"run_id": "r-push", "ts": "t", "text": "pushed earlier",
                                   "pushed": True, "surfaced": False})
    payload = json.dumps({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "resume"})
    import io, sys
    sys.stdin = io.StringIO(payload)
    try:
        assert cli.main(["_session-hook", "session-start"]) == 0
    finally:
        sys.stdin = sys.__stdin__
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "pushed earlier" in out["hookSpecificOutput"]["additionalContext"]
    sys.stdin = io.StringIO(json.dumps({"session_id": SESSION}))
    try:
        assert cli.main(["_session-hook", "user-prompt"]) == 0
    finally:
        sys.stdin = sys.__stdin__
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- CLI: notify / sessions

def test_notify_and_sessions_commands(registry, monkeypatch, capsys):
    _transcript(registry["claude_dir"], "default")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    assert cli.main(["notify", "hello there"]) == 0
    assert "delivered to tariff-lane" in capsys.readouterr().out
    lines = _wait_lines(registry["inbox"], 2)
    assert 'from-mode="prompting"' in lines[1]["message"]["content"]
    assert cli.main(["notify", "--session", "ghost", "x"]) == 1
    assert "NOT delivered" in capsys.readouterr().out
    assert cli.main(["sessions"]) == 0
    table = capsys.readouterr().out
    assert "tariff-lane" in table and SESSION in table and "(this)" in table
    assert cli.main(["sessions", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["session_id"] == SESSION


def test_finish_skips_push_while_inline_waiter_is_alive(registry, tmp_path):
    _transcript(registry["claude_dir"], "bypassPermissions")
    workdir = tmp_path / "w"
    workdir.mkdir()
    prompt = tmp_path / "p.md"
    prompt.write_text("x\n")
    out = tmp_path / "o.md"
    out.write_text("done\n")
    alive = run_ledger.start_run(family="codex", model="m", lane="/l", workdir=workdir, prompt=prompt, out=out,
                                 caller={"session_id": SESSION, "waiter_pid": os.getpid()})
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", alive, "--rc", "0"]) == 0
    _, meta = run_ledger.load_run(alive)
    assert meta["notify"]["pushed"] is False
    assert meta["notify"]["push"]["reason"] == "inline-waiter-alive"
    assert registry["inbox"].lines == []
    assert notify.pending_notices(SESSION) == []

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone = run_ledger.start_run(family="codex", model="m", lane="/l", workdir=workdir, prompt=prompt, out=out,
                                caller={"session_id": SESSION, "waiter_pid": dead.pid})
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", gone, "--rc", "0"]) == 0
    _, meta = run_ledger.load_run(gone)
    assert meta["notify"]["pushed"] is True
    assert _wait_lines(registry["inbox"], 2)[1]["type"] == "user"

def test_lane_sessions_are_hidden_and_refused_unless_forced(registry, tmp_path, monkeypatch):
    """2026-09-04: a routing broadcast pushed into two RUNNING lane sessions
    became their last message and therefore their captured .out. Lanes are
    hidden from the registry listing by default and refused as notify targets
    unless forced; interactive sessions are unaffected."""
    claude_dir = registry["claude_dir"]
    sessions = claude_dir / "sessions"
    lane_id = "cccccccc-3333-4000-8000-000000000001"
    # a live registry row for the lane (same pid, alive by construction) with a
    # transcript whose first prompt came from the SDK
    (sessions / f"{registry['pid']}.lane.json").write_text(json.dumps({
        "pid": registry["pid"], "sessionId": lane_id, "cwd": str(tmp_path / "work"),
        "startedAt": 1787500000001, "messagingSocketPath": str(registry["socket"]), "name": "lane-run",
    }))
    projects = claude_dir / "projects" / "-Users-max-work"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f"{lane_id}.jsonl").write_text(json.dumps({
        "type": "user", "uuid": "u1", "sessionId": lane_id, "entrypoint": "sdk-cli",
        "promptSource": "sdk", "cwd": str(tmp_path / "work"),
        "message": {"role": "user", "content": "# lane brief"},
    }) + "\n")
    monkeypatch.setattr(notify, "find_session", lambda sid: {
        "pid": registry["pid"], "socket": str(registry["socket"]), "name": "lane-run",
        "alive": True, "socket_present": True,
    } if sid == lane_id else notify.find_session.__wrapped__(sid) if hasattr(notify.find_session, "__wrapped__") else None)

    listed = {row["session_id"]: row for row in notify.live_sessions()}
    assert lane_id not in listed and SESSION in listed
    everything = {row["session_id"]: row for row in notify.live_sessions(include_lanes=True)}
    assert everything[lane_id]["lane"] is True and everything[SESSION]["lane"] is False

    refused = notify.push_to_session(lane_id, "routing broadcast")
    assert refused["delivered"] is False and refused["reason"].startswith("lane-session")
    forced = notify.push_to_session(lane_id, "operator override", force=True)
    assert "lane-session" not in (forced.get("reason") or "")
