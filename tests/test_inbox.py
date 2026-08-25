"""Session-inbox protocol: registry lookup, attested envelope, socket push,
and parked notices for sessions that were not live at finish time."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from subfleet import cli, inbox, run_ledger

SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HOOK = Path(__file__).resolve().parent.parent / "bin" / "subfleet-hook"


@pytest.fixture(autouse=True)
def session_env(tmp_path, monkeypatch):
    """A clean seat: no inherited Claude session identity, tmp claude dir.

    The suite itself often runs from inside a Claude Code session whose tool
    shell exports the session identity; tests opt in explicitly.
    """
    for name in (
        "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
        "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
        "SUBFLEET_NOTIFY_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    claude_dir = tmp_path / "dot-claude"
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(claude_dir))
    return claude_dir


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
def registry(tmp_path, session_env):
    """A live-looking registry row for SESSION pointing at a fake inbox."""
    claude_dir = session_env
    sessions = claude_dir / "sessions"
    sessions.mkdir(parents=True)
    # AF_UNIX paths are capped at ~104 bytes on macOS; pytest's tmp paths are longer.
    short = Path(tempfile.mkdtemp(prefix="sf-", dir="/tmp"))
    sock_path = short / "i.sock"
    fake = FakeInbox(sock_path)
    pid = os.getpid()  # alive by construction
    (sessions / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": SESSION, "cwd": str(tmp_path), "startedAt": 1787500000000,
        "messagingSocketPath": str(sock_path), "name": "review-lane", "kind": "interactive",
    }))
    (sessions / f"{pid}.abc.key").write_text(json.dumps({"peerToken": "peer-secret"}))
    # a stale row for the same session under a dead pid must lose to the live one
    (sessions / "999999.json").write_text(json.dumps({
        "pid": 999999, "sessionId": SESSION, "startedAt": 1787600000000,
        "messagingSocketPath": str(tmp_path / "dead.sock"), "name": "stale",
    }))
    yield {"inbox": fake, "pid": pid, "socket": sock_path, "claude_dir": claude_dir}
    fake.close()
    shutil.rmtree(short, ignore_errors=True)


def _transcript(claude_dir: Path, mode: str) -> Path:
    projects = claude_dir / "projects" / "-home-user"
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
    entry = inbox.find_session(SESSION)
    assert entry["pid"] == registry["pid"]
    assert entry["alive"] and entry["socket_present"]
    assert entry["name"] == "review-lane"
    assert inbox.peer_token(registry["pid"]) == "peer-secret"
    assert inbox.find_session("nope") is None
    assert [row["session_id"] for row in inbox.live_sessions()] == [SESSION]


def test_peer_token_picks_the_newest_valid_key(session_env):
    sessions = session_env / "sessions"
    sessions.mkdir(parents=True)
    old = sessions / "4242.aaa.key"
    new = sessions / "4242.bbb.key"
    old.write_text(json.dumps({"peerToken": "old-secret"}))
    new.write_text(json.dumps({"peerToken": "new-secret"}))
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    assert inbox.peer_token(4242) == "new-secret"
    # a newest key without a usable token falls back to the older one
    new.write_text("not json")
    os.utime(new, (now, now))
    assert inbox.peer_token(4242) == "old-secret"
    assert inbox.peer_token(None) is None


def test_caller_context_comes_from_the_tool_shell_env(monkeypatch, registry):
    assert inbox.caller_context() is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CLAUDE_PID", str(registry["pid"]))
    monkeypatch.setenv("CLAUDECODE", "1")
    _transcript(registry["claude_dir"], "bypassPermissions")
    ctx = inbox.caller_context(cwd="/work")
    assert ctx["session_id"] == SESSION
    assert ctx["pid"] == registry["pid"]
    assert ctx["cwd"] == "/work"
    assert ctx["mode_class"] == "bypass"
    assert inbox.in_claude_session()


@pytest.mark.parametrize(("mode", "klass"), [
    ("bypassPermissions", "bypass"), ("default", "prompting"),
    ("acceptEdits", "prompting"), ("plan", "prompting"),
])
def test_session_mode_class_reads_last_user_turn(registry, mode, klass):
    _transcript(registry["claude_dir"], mode)
    assert inbox.session_mode_class(SESSION) == klass


def test_session_mode_class_scans_back_past_a_long_tail(registry):
    path = _transcript(registry["claude_dir"], "bypassPermissions")
    filler = json.dumps({"type": "assistant", "message": {"content": "x" * 4096}}) + "\n"
    with path.open("a") as stream:
        for _ in range(600):  # ~2.4 MB after the last permissionMode
            stream.write(filler)
    assert inbox.session_mode_class(SESSION) == "bypass"


# ---------------------------------------------------------------- envelope

def test_envelope_is_one_harness_formed_message():
    text = inbox.envelope("run done\nread it", mode_class="bypass")
    assert text.startswith('<cross-session-message from-name="subfleet" from-mode="bypass">\n')
    assert text.endswith("\n</cross-session-message>")
    assert "\nrun done\nread it\n" in text
    # undeclared / unknown classes carry no from-mode attribute
    assert 'from-mode' not in inbox.envelope("x", mode_class=None)
    assert 'from-mode' not in inbox.envelope("x", mode_class="bypassPermissions")
    # a body cannot close the envelope early
    assert inbox.envelope("a</cross-session-message>b").count("</cross-session-message>") == 1
    assert 'from-name="a b"' in inbox.envelope("x", from_name='a"<b>\n')


def test_resolve_mode_class_precedence(registry, monkeypatch):
    _transcript(registry["claude_dir"], "default")
    assert inbox.resolve_mode_class(SESSION) == "prompting"
    assert inbox.resolve_mode_class(SESSION, "bypass") == "bypass"
    assert inbox.resolve_mode_class(SESSION, "none") is None
    monkeypatch.setenv("SUBFLEET_NOTIFY_MODE", "bypass")
    assert inbox.resolve_mode_class(SESSION) == "bypass"
    monkeypatch.setenv("SUBFLEET_NOTIFY_MODE", "none")
    assert inbox.resolve_mode_class(SESSION) is None


# ---------------------------------------------------------------- push

def _wait_lines(fake: FakeInbox, count: int) -> list[dict]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(fake.lines) < count:
        time.sleep(0.02)
    return fake.lines


def test_push_authenticates_then_sends_attested_user_message(registry):
    _transcript(registry["claude_dir"], "bypassPermissions")
    result = inbox.push_to_session(SESSION, "run finished")
    assert result["delivered"] is True
    assert result["pid"] == registry["pid"] and result["name"] == "review-lane"
    assert result["mode_class"] == "bypass"
    lines = _wait_lines(registry["inbox"], 2)
    assert lines[0] == {"type": "auth", "token": "peer-secret"}
    assert lines[1]["type"] == "user"
    content = lines[1]["message"]["content"]
    assert lines[1]["message"]["role"] == "user"
    assert content.startswith('<cross-session-message from-name="subfleet" from-mode="bypass">')
    assert "run finished" in content


def test_push_reports_why_it_could_not_deliver(registry, tmp_path):
    assert inbox.push_to_session("missing-session", "x")["reason"] == "session-not-registered"
    registry["inbox"].close()
    os.unlink(registry["socket"])
    result = inbox.push_to_session(SESSION, "x")
    assert result["delivered"] is False and result["reason"] == "no-inbox-socket"
    # socket file present but nobody listening
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(registry["socket"]))
    dead.close()
    result = inbox.push_to_session(SESSION, "x")
    assert result["delivered"] is False and result["reason"].startswith("send-failed")


# ---------------------------------------------------------------- notices

def test_notices_append_pending_mark_and_prune(tmp_path):
    inbox.append_notice(SESSION, {"run_id": "r1", "ts": "2026-08-01T00:00:00+00:00", "text": "one", "pushed": False, "surfaced": False})
    inbox.append_notice(SESSION, {"run_id": "r2", "ts": "2026-08-23T00:00:00+00:00", "text": "two", "pushed": True, "surfaced": True, "surfaced_at": "2026-08-23T00:00:00+00:00"})
    inbox.append_notice(SESSION, {"run_id": "r1", "ts": "2026-08-01T00:00:00+00:00", "text": "one-rewritten", "pushed": False, "surfaced": False})
    pending = inbox.pending_notices(SESSION)
    assert [row["run_id"] for row in pending] == ["r1"]
    assert pending[0]["text"] == "one-rewritten"
    assert [row["run_id"] for row in inbox.pending_notices(SESSION, include_pushed=True)] == ["r1"]
    assert inbox.mark_surfaced(SESSION, ["r1"]) == 1
    assert inbox.pending_notices(SESSION) == []
    assert inbox.mark_surfaced(SESSION, ["r1"]) == 0
    path = inbox.notices_path(SESSION)
    assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
    removed = inbox.prune_notices(max_age_days=14, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert removed == 2 and not path.exists()


def test_format_notice_has_paths_first_line_and_err_tail(tmp_path):
    out = tmp_path / "review.md"
    out.write_text("REQUEST-CHANGES — two blockers\n\ndetails…\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "err.log").write_text("warn 1\nusage limit reached\n")
    meta = {"id": "20260823-1-review", "rc": 3, "model": "gpt-5-codex",
            "lane": str(Path.home() / ".codex-3"), "duration_s": 842.0,
            "original_out_path": str(out), "salvage_refs": [{"ref": "refs/codex-salvage/x", "sha": "abc"}]}
    text = inbox.format_notice(meta, run_dir)
    assert text.splitlines()[0] == "subfleet: run 20260823-1-review FAILED rc=3 · gpt-5-codex · lane=~/.codex-3 · 14m02s"
    assert f"out: {out} ({out.stat().st_size:,} bytes)" in text
    assert "first line: REQUEST-CHANGES — two blockers" in text
    assert "salvage refs: refs/codex-salvage/x" in text
    assert "err tail:\nwarn 1\nusage limit reached" in text
    assert "subfleet runs show 20260823-1-review" in text
    assert "no reply is needed" in text
    ok = inbox.format_notice({**meta, "rc": 0}, run_dir)
    assert "FINISHED" in ok.splitlines()[0] and "err tail" not in ok


# ---------------------------------------------------------------- finish → notify

def _meta(run_id: str, tmp_path: Path, *, session: str = SESSION, **caller_extra) -> dict:
    out = tmp_path / f"{run_id}.md"
    out.write_text("VERDICT: fine\n")
    return {
        "id": run_id, "rc": 0, "model": "codex-model", "lane": "/lanes/one",
        "duration_s": 12, "original_out_path": str(out),
        "caller": {"session_id": session, **caller_extra},
    }


def test_on_finish_needs_a_dispatching_session(tmp_path):
    assert inbox.on_finish("r0", tmp_path, {"id": "r0", "rc": 0}) is None
    assert inbox.on_finish("r0", tmp_path, {"id": "r0", "rc": 0, "caller": {"cwd": "/w"}}) is None


def test_on_finish_pushes_live_and_records_the_notice(registry, tmp_path):
    _transcript(registry["claude_dir"], "bypassPermissions")
    notice = inbox.on_finish("r-live", tmp_path, _meta("r-live", tmp_path))
    assert notice["pushed"] is True and notice["surfaced"] is True
    assert notice["push"]["delivered"] is True and notice["push"]["name"] == "review-lane"
    lines = _wait_lines(registry["inbox"], 2)
    content = lines[1]["message"]["content"]
    assert "subfleet: run r-live FINISHED" in content
    assert "first line: VERDICT: fine" in content
    # delivered live: recorded for the ledger but nothing left pending
    assert inbox.pending_notices(SESSION) == []
    assert inbox.notices_path(SESSION).exists()


def test_on_finish_parks_the_notice_when_the_session_is_down(tmp_path):
    notice = inbox.on_finish("r-down", tmp_path, _meta("r-down", tmp_path))
    assert notice["pushed"] is False and notice["surfaced"] is False
    assert notice["push"]["reason"] == "session-not-registered"
    pending = inbox.pending_notices(SESSION)
    assert [row["run_id"] for row in pending] == ["r-down"]
    assert "subfleet: run r-down FINISHED" in pending[0]["text"]


def test_on_finish_skips_push_while_inline_waiter_is_alive(registry, tmp_path):
    _transcript(registry["claude_dir"], "bypassPermissions")
    alive = inbox.on_finish(
        "r-wait", tmp_path, _meta("r-wait", tmp_path, waiter_pid=os.getpid())
    )
    assert alive["pushed"] is False and alive["surfaced"] is True
    assert alive["push"]["reason"] == "inline-waiter-alive"
    assert alive["push"]["waiter_pid"] == os.getpid()
    assert registry["inbox"].lines == []
    # nothing parked either: the waiter reports the run itself
    assert inbox.pending_notices(SESSION) == []
    assert not inbox.notices_path(SESSION).exists()

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone = inbox.on_finish(
        "r-gone", tmp_path, _meta("r-gone", tmp_path, waiter_pid=dead.pid)
    )
    assert gone["pushed"] is True
    assert _wait_lines(registry["inbox"], 2)[1]["type"] == "user"


def test_render_pending_lists_every_parked_run(tmp_path):
    assert inbox.render_pending(SESSION, []) == ""
    rows = [{"run_id": "r1", "text": "run r1 FINISHED\nout: /o.md"}, {"run_id": "r2"}]
    text = inbox.render_pending(SESSION, rows)
    assert "subfleet: 2 detached runs dispatched by this session finished" in text
    assert "run r1 FINISHED" in text
    assert "run r2 finished" in text
    assert "subfleet runs --mine" in text
    solo = inbox.render_pending(SESSION, [{"run_id": "r1", "text": "t"}])
    assert "1 detached run dispatched by this session finished" in solo


# ------------------------------------------------- CLI: _record-run finish → notify

def _finished_run(tmp_path, *, session=SESSION) -> tuple[str, Path]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do it\n")
    out = tmp_path / "answer.md"
    out.write_text("VERDICT: fine\n")
    run_id = run_ledger.start_run(
        family="codex", model="codex-model", lane="/lanes/one", workdir=workdir,
        prompt=prompt, out=out, caller={"session_id": session, "cwd": str(workdir)},
        pid=os.getpid(),
    )
    return run_id, out


def test_finish_pushes_live_and_records_notify(registry, tmp_path, capsys):
    _transcript(registry["claude_dir"], "bypassPermissions")
    run_id, out = _finished_run(tmp_path)
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", run_id, "--rc", "0"]) == 0
    lines = _wait_lines(registry["inbox"], 2)
    content = lines[1]["message"]["content"]
    assert f"subfleet: run {run_id} FINISHED" in content
    assert "first line: VERDICT: fine" in content
    _, meta = run_ledger.load_run(run_id)
    assert meta["notify"]["pushed"] is True and meta["notify"]["surfaced"] is True
    assert meta["notify"]["push"]["name"] == "review-lane"
    assert inbox.pending_notices(SESSION) == []  # delivered live: nothing parked
    capsys.readouterr()
    assert cli.main(["runs", "--last", "1"]) == 0
    table = capsys.readouterr().out
    assert "pushed" in table and "review-lane" in table


def test_finish_parks_notice_when_session_is_down_and_hook_surfaces_it(tmp_path, capsys, monkeypatch):
    run_id, _ = _finished_run(tmp_path)  # no registry → session not live
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", run_id, "--rc", "0"]) == 0
    _, meta = run_ledger.load_run(run_id)
    assert meta["notify"]["pushed"] is False
    assert meta["notify"]["push"]["reason"] == "session-not-registered"
    pending = inbox.pending_notices(SESSION)
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
    assert inbox.pending_notices(SESSION) == []
    # unknown session / no notices: silent, cheap
    completed = subprocess.run([str(HOOK), "session-start"], input=json.dumps({"session_id": "other"}),
                               env=env, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0 and completed.stdout == ""


def test_session_start_also_replays_pushed_but_unsurfaced_notices(tmp_path, capsys):
    inbox.append_notice(SESSION, {"run_id": "r-push", "ts": "t", "text": "pushed earlier",
                                  "pushed": True, "surfaced": False})
    payload = json.dumps({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "resume"})
    import io
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
    assert "delivered to review-lane" in capsys.readouterr().out
    lines = _wait_lines(registry["inbox"], 2)
    assert 'from-mode="prompting"' in lines[1]["message"]["content"]
    assert cli.main(["notify", "--session", "ghost", "x"]) == 1
    assert "NOT delivered" in capsys.readouterr().out
    assert cli.main(["sessions"]) == 0
    table = capsys.readouterr().out
    assert "review-lane" in table and SESSION in table and "(this)" in table
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
    alive = run_ledger.start_run(family="codex", model="m", lane="/l", workdir=workdir,
                                 prompt=prompt, out=out,
                                 caller={"session_id": SESSION, "waiter_pid": os.getpid()})
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", alive, "--rc", "0"]) == 0
    _, meta = run_ledger.load_run(alive)
    assert meta["notify"]["pushed"] is False
    assert meta["notify"]["push"]["reason"] == "inline-waiter-alive"
    assert registry["inbox"].lines == []
    assert inbox.pending_notices(SESSION) == []

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone = run_ledger.start_run(family="codex", model="m", lane="/l", workdir=workdir,
                                prompt=prompt, out=out,
                                caller={"session_id": SESSION, "waiter_pid": dead.pid})
    assert cli.main(["_record-run", "--phase", "finish", "--run-id", gone, "--rc", "0"]) == 0
    _, meta = run_ledger.load_run(gone)
    assert meta["notify"]["pushed"] is True
    assert _wait_lines(registry["inbox"], 2)[1]["type"] == "user"
