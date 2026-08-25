"""`subfleet wait`, `subfleet kill`, `subfleet runs reap|--mine|--running`, and the
runners adopting a run id that `subfleet run` pre-created.

Ledger-level tests exercise ``subfleet.run_ledger`` directly and run standalone.
Tests named ``*_cli_*`` need the wait/kill/runs subcommands in ``subfleet.cli``;
tests named ``test_codex_runner_*`` / ``test_claude_runner_*`` need the runner
scripts' adopt/detach support (``SUBFLEET_RUN_ID``, ``-A``, ``-d``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet import cli, config, run_ledger

SESSION = "12121212-3434-4565-8787-909090909090"
ROOT = Path(__file__).resolve().parents[1]
CODEX_RUNNER = ROOT / "bin" / "subfleet-codex"
CLAUDE_RUNNER = ROOT / "bin" / "subfleet-claude"
SUBFLEET = ROOT / "bin" / "subfleet"

# Session identity and run adoption markers may leak in from an outer Claude
# session or dispatcher; every test here opts in to them explicitly.
_AMBIENT = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "SUBFLEET_RUN_DETACH",
    "SUBFLEET_RUN_ID",
    "SUBFLEET_RUN_CALLER_JSON",
    "SUBFLEET_RUN_LANE_LOG",
    "SUBFLEET_RUN_OWNED_PROMPT",
    "SUBFLEET_NOTIFY_MODE",
    "SUBFLEET_CODEX_DETACHED",
)


@pytest.fixture(autouse=True)
def _no_ambient_session(monkeypatch):
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)


def _start(tmp_path, name="job", *, session=SESSION, pid=None, out=None, started=None):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / f"{name}.prompt.md"
    prompt.write_text("do it\n")
    return run_ledger.start_run(
        family="codex",
        model="gpt-5.6-sol",
        lane="/lanes/one",
        workdir=workdir,
        prompt=prompt,
        out=out,
        caller={"session_id": session} if session else None,
        pid=pid,
        slug=name,
        launcher="subfleet run",
        started=started,
    )


# --- ledger level -----------------------------------------------------------


def test_start_run_with_out_none_hosts_artifacts_in_the_run_dir(tmp_path):
    run_id = _start(tmp_path, "hosted")
    artifact_paths = run_ledger.run_paths(run_id)
    run_dir = Path(artifact_paths["run_dir"])
    assert artifact_paths["out"] == str(run_dir / "out.md")
    assert artifact_paths["err"] == str(run_dir / "out.err.log")
    assert artifact_paths["lane_log"] == str(run_dir / "out.lane.log")
    _, meta = run_ledger.load_run(run_id)
    assert meta["out_path"] == artifact_paths["out"]
    assert meta["original_out_path"] is None
    assert meta["caller"] == {"session_id": SESSION}
    assert meta["launcher"] == "subfleet run"
    assert run_id.endswith("-hosted")

    Path(artifact_paths["out"]).write_text("hosted answer\n")
    run_ledger.finish_run(run_id, rc=0)
    run_dir, meta = run_ledger.load_run(run_id)
    # finish copies out onto itself here; the samefile guard must not truncate
    assert (run_dir / "out.md").read_text() == "hosted answer\n"
    assert run_ledger.output_path(meta) == str(run_dir / "out.md")


def test_adopt_run_repoints_artifacts_and_refuses_finished(tmp_path):
    run_id = _start(tmp_path, "adoptee")
    new_out = tmp_path / "runner-out.md"
    adopted = run_ledger.adopt_run(
        run_id,
        lane="/lanes/two",
        pid=os.getpid(),
        out=new_out,
        err=tmp_path / "runner.err.log",
        lane_log=tmp_path / "runner.lane.log",
    )
    assert adopted == run_id
    _, meta = run_ledger.load_run(run_id)
    assert meta["lane"] == "/lanes/two" and meta["pid"] == os.getpid()
    assert meta["out_path"] == str(new_out) and meta["adopted_at"]
    assert run_ledger.run_paths(run_id)["out"] == str(new_out)

    new_out.write_text("runner answer\n")
    run_ledger.finish_run(run_id, rc=0)
    run_dir, meta = run_ledger.load_run(run_id)
    assert (run_dir / "out.md").read_text() == "runner answer\n"
    assert run_ledger.output_path(meta) == str(new_out)
    with pytest.raises(ValueError):
        run_ledger.adopt_run(run_id)
    with pytest.raises(FileNotFoundError):
        run_ledger.adopt_run("does-not-exist")


def test_wait_for_runs_blocks_until_finish(tmp_path):
    run_id = _start(tmp_path, "slow", pid=os.getpid())
    out_path = Path(run_ledger.run_paths(run_id)["out"])
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            out_path.write_text("answer\n")
            run_ledger.finish_run(run_id, rc=0)

    done = run_ledger.wait_for_runs([run_id], interval=0.01, sleep=fake_sleep)
    assert done[run_id]["rc"] == 0 and len(sleeps) == 2
    assert "FINISHED" in run_ledger.summary_line(run_id, done[run_id])

    failed = _start(tmp_path, "bad", pid=os.getpid())
    run_ledger.finish_run(failed, rc=3)
    done = run_ledger.wait_for_runs([failed], interval=0.01, sleep=lambda _s: None)
    assert done[failed]["rc"] == 3
    assert "FAILED rc=3" in run_ledger.summary_line(failed, done[failed])


def test_wait_for_runs_times_out_and_detects_orphans(tmp_path):
    running = _start(tmp_path, "forever", pid=os.getpid())
    clock = [0.0]

    def fake_sleep(seconds):
        clock[0] += seconds

    done = run_ledger.wait_for_runs(
        [running], timeout=1.0, interval=0.5, sleep=fake_sleep, clock=lambda: clock[0]
    )
    assert done == {}

    # a runner pid that is dead without a finish record → ORPHANED after the grace
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    orphan = _start(tmp_path, "orphan", pid=child.pid)
    done = run_ledger.wait_for_runs(
        [orphan], interval=0.5, orphan_grace=1.0, sleep=fake_sleep, clock=lambda: clock[0]
    )
    assert done[orphan]["orphaned"] is True
    assert "ORPHANED" in run_ledger.summary_line(orphan, done[orphan])

    done = run_ledger.wait_for_runs(["does-not-exist"], interval=0.01, sleep=lambda _s: None)
    assert done["does-not-exist"]["missing"] is True
    assert "UNKNOWN" in run_ledger.summary_line("does-not-exist", done["does-not-exist"])


def test_list_runs_filters_by_session_and_running_and_latest(tmp_path):
    mine = _start(tmp_path, "mine", pid=os.getpid(), started="2026-08-22T10:00:00+00:00")
    other = _start(
        tmp_path, "other", session="someone-else", started="2026-08-22T10:01:00+00:00"
    )
    run_ledger.finish_run(other, rc=0)

    rows = run_ledger.list_runs(20, session_id=SESSION)
    assert [row["id"] for row in rows] == [mine]
    assert rows[0]["caller_session"] == SESSION and rows[0]["launcher"] == "subfleet run"
    assert rows[0]["status"] == "RUNNING" and rows[0]["notify"] == "pending"
    assert run_ledger.latest_run_id(session_id=SESSION) == mine
    assert [row["id"] for row in run_ledger.list_runs(20, running_only=True)] == [mine]

    run_ledger.finish_run(mine, rc=0)
    assert run_ledger.list_runs(20, running_only=True) == []
    assert run_ledger.list_runs(20, session_id=SESSION)[0]["notify"] == "none"
    run_ledger.set_notify(mine, {"pushed": False, "surfaced": False})
    assert run_ledger.list_runs(20, session_id=SESSION)[0]["notify"] == "parked"
    run_ledger.set_notify(mine, {"pushed": True})
    assert run_ledger.list_runs(20, session_id=SESSION)[0]["notify"] == "pushed"

    nocaller = _start(tmp_path, "nocaller", session=None, started="2026-08-22T10:02:00+00:00")
    run_ledger.finish_run(nocaller, rc=0)
    newest = run_ledger.list_runs(1)[0]
    assert newest["id"] == nocaller and newest["notify"] is None
    assert run_ledger.latest_run_id() == nocaller

    rendered = run_ledger.format_runs(
        run_ledger.list_runs(), session_names={SESSION: "boss"}
    )
    assert "boss" in rendered and "pushed" in rendered


def test_kill_run_signals_runner_and_reap_finalizes_dead_ones(tmp_path):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    live = _start(tmp_path, "live", pid=sleeper.pid)
    result = run_ledger.kill_run(live)
    assert result["status"] == "signalled" and result["pid"] == sleeper.pid
    assert sleeper.wait(timeout=5) == -signal.SIGTERM

    # reap: the pid is gone and no finish record was written (no trap ran)
    time.sleep(0.05)
    assert run_ledger.reap_orphans(grace_s=0, dry_run=True) == [live]
    assert run_ledger.load_run(live)[1]["finished_at"] is None
    assert run_ledger.reap_orphans(grace_s=0) == [live]
    _, meta = run_ledger.load_run(live)
    assert meta["rc"] == -9 and meta["orphaned"] is True and meta["finished_at"]
    assert run_ledger.kill_run(live)["status"] == "already-finished"

    no_pid = _start(tmp_path, "nopid")
    assert run_ledger.kill_run(no_pid)["status"] == "no-pid"
    assert run_ledger.reap_orphans(grace_s=0) == []  # no pid → never reaped

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    fresh = _start(tmp_path, "fresh", pid=child.pid)
    assert run_ledger.reap_orphans(grace_s=3600) == []  # inside the grace window
    assert run_ledger.kill_run(fresh)["status"] == "orphaned"


# --- CLI level (subfleet wait / kill / runs) --------------------------------


def test_wait_cli_reports_rc_and_cats_output(tmp_path, capsys):
    run_id = _start(tmp_path, "slow", pid=os.getpid())
    Path(run_ledger.run_paths(run_id)["out"]).write_text("answer\n")
    run_ledger.finish_run(run_id, rc=0)
    assert cli.main(["wait", run_id, "--cat"]) == 0
    captured = capsys.readouterr()
    assert f"subfleet wait: {run_id} FINISHED" in captured.out
    assert "answer" in captured.out

    failed = _start(tmp_path, "bad", pid=os.getpid())
    run_ledger.finish_run(failed, rc=3)
    assert cli.main(["wait", failed, run_id]) == 3
    assert "FAILED rc=3" in capsys.readouterr().out


def test_wait_cli_times_out_and_reports_unknown_ids(tmp_path, capsys):
    running = _start(tmp_path, "forever", pid=os.getpid())
    assert cli.main(["wait", running, "--timeout", "0.01", "--interval", "0.01"]) == 124
    assert "still RUNNING" in capsys.readouterr().out

    # an orphan inside the 30s grace still reads RUNNING; the bounded timeout
    # keeps the suite fast (ledger-level orphan detection uses a fake clock)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    orphan = _start(tmp_path, "orphan", pid=child.pid)
    assert cli.main(["wait", orphan, "--timeout", "0.05", "--interval", "0.01"]) == 124
    capsys.readouterr()

    assert cli.main(["wait", "does-not-exist"]) == 2
    assert "UNKNOWN" in capsys.readouterr().out


def test_wait_cli_mine_and_last_resolve_ids_from_the_session(tmp_path, monkeypatch, capsys):
    mine = _start(tmp_path, "mine", pid=os.getpid())
    other = _start(tmp_path, "other", session="someone-else", pid=os.getpid())
    run_ledger.finish_run(mine, rc=0)
    run_ledger.finish_run(other, rc=0)
    assert cli.main(["wait", "--mine"]) == 2  # no session id → usage error
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    assert cli.main(["wait", "--mine"]) == 0  # nothing running: fine
    assert cli.main(["wait", "--last", "--mine"]) == 0
    assert mine in capsys.readouterr().out
    assert cli.main(["runs", "--mine", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == [mine]
    assert rows[0]["caller_session"] == SESSION and rows[0]["launcher"] == "subfleet run"
    assert cli.main(["runs", "--running", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_kill_cli_and_runs_reap_report(tmp_path, capsys):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    live = _start(tmp_path, "live", pid=sleeper.pid)
    assert cli.main(["kill", live]) == 0
    assert f"{live}: signalled pid={sleeper.pid}" in capsys.readouterr().out
    assert sleeper.wait(timeout=5) == -signal.SIGTERM

    time.sleep(0.05)
    assert cli.main(["runs", "reap", "--dry-run", "--grace", "0"]) == 0
    assert "would finalize 1" in capsys.readouterr().out
    assert run_ledger.reap_orphans(grace_s=0) == [live]
    assert cli.main(["kill", live]) == 0
    assert "already-finished" in capsys.readouterr().out
    no_pid = _start(tmp_path, "nopid")
    assert cli.main(["kill", no_pid]) == 1
    assert "no-pid" in capsys.readouterr().out


# --- runner level (bin/subfleet-codex, bin/subfleet-claude) -----------------


def _fake_codex(path: Path) -> None:
    path.write_text(
        """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
if [ -n "${FAKE_LIMIT_HOME:-}" ] && [ "$CODEX_HOME" = "$FAKE_LIMIT_HOME" ]; then
  echo "You've hit your usage limit. Try again at 11:33 PM." >&2
  exit 1
fi
printf 'codex says hi from %s\\n' "$CODEX_HOME" > "$out"
"""
    )
    path.chmod(0o755)


def _runner_env(fake_bin: Path, state: Path) -> dict:
    env = os.environ.copy()
    for name in _AMBIENT:
        env.pop(name, None)
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "SUBFLEET_CODEX_GUARD": "off",
            "SUBFLEET_CODEX_BIN": str(fake_bin / "codex"),
            "SUBFLEET_RUN_SUBFLEET": str(SUBFLEET),
            "SUBFLEET_STATE_DIR": str(state),
        }
    )
    return env


def test_codex_runner_adopts_precreated_run_and_records_pid(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    state = tmp_path / "state"
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(state))
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("task\n")
    out = tmp_path / "answer.md"
    run_id = run_ledger.start_run(
        family="codex",
        model="gpt-test",
        lane=None,
        workdir=workdir,
        prompt=prompt,
        out=out,
        caller={"session_id": SESSION},
        slug="adopted",
        launcher="subfleet run",
    )
    env = _runner_env(fake_bin, state)
    env["SUBFLEET_RUN_ID"] = run_id
    env["SUBFLEET_RUN_LANE_LOG"] = str(tmp_path / "answer.lane.log")
    completed = subprocess.run(
        [
            str(CODEX_RUNNER), "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    runs = sorted((state / "runs").glob("*/meta.json"))
    assert len(runs) == 1, "the runner must adopt, not start a second record"
    meta = json.loads(runs[0].read_text())
    assert meta["id"] == run_id and meta["rc"] == 0
    assert meta["lane"] == str(home) and isinstance(meta["pid"], int) and meta["pid"] > 0
    assert meta["adopted_at"] and meta["caller"]["session_id"] == SESSION
    assert meta["notify"]["pushed"] is False  # no live session in the test registry
    assert (runs[0].parent / "out.md").read_text().startswith("codex says hi")

    # a finished id is refused: the runner starts its own record instead
    completed = subprocess.run(
        [
            str(CODEX_RUNNER), "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert len(list((state / "runs").glob("*/meta.json"))) == 2


def test_codex_runner_pinned_lane_with_auto_repicks_on_limit(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    limited = tmp_path / "lane-limited"
    fresh = tmp_path / "lane-fresh"
    limited.mkdir()
    fresh.mkdir()
    picker = fake_bin / "pick"
    picker.write_text(f"#!/bin/bash\nprintf '%s\\n' '{fresh}'\n")
    picker.chmod(0o755)
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("task\n")
    out = tmp_path / "answer.md"
    env = _runner_env(fake_bin, tmp_path / "state")
    env["FAKE_LIMIT_HOME"] = str(limited)
    env["SUBFLEET_CODEX_PICK"] = str(picker)
    pinned = subprocess.run(
        [
            str(CODEX_RUNNER), "-H", str(limited), "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out), "-r", "2",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert pinned.returncode != 0 and "re-picked" not in pinned.stderr
    auto = subprocess.run(
        [
            str(CODEX_RUNNER), "-H", str(limited), "-A", "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(out), "-r", "2",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert auto.returncode == 0, auto.stderr
    assert "will re-pick on a usage limit (-A)" in auto.stderr
    assert "re-picked" in auto.stderr
    assert out.read_text().strip() == f"codex says hi from {fresh}"


def test_codex_runner_detach_runs_in_a_new_process_session(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text(
        """#!/bin/bash
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
python3 -c 'import os; print(os.getsid(0), os.getpgid(0))' > "$out"
"""
    )
    codex.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("task\n")
    out = tmp_path / "answer.md"
    env = _runner_env(fake_bin, tmp_path / "state")
    completed = subprocess.run(
        [
            str(CODEX_RUNNER), "-d", "-H", str(home), "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("subfleet codex: detached pid=")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if out.exists() and out.read_text().strip():
            break
        time.sleep(0.05)
    sid, pgid = (int(x) for x in out.read_text().split())
    assert sid != os.getsid(0), "detached runner must not share the launcher's session"
    assert pgid != os.getpgid(0)
    lane_log = tmp_path / "answer.lane.log"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if lane_log.exists() and "subfleet codex: OK" in lane_log.read_text():
            break
        time.sleep(0.05)
    assert "subfleet codex: OK" in lane_log.read_text()


def test_claude_runner_adopts_run_and_cleans_delegate_prompt(tmp_path, monkeypatch):
    from test_carpool_claude import _fixture

    state = tmp_path / "state"
    monkeypatch.setenv("SUBFLEET_STATE_DIR", str(state))
    env, paths = _fixture(
        tmp_path, "printf '{\"is_error\":false,\"result\":\"adopted answer\"}\\n'\n"
    )
    for name in _AMBIENT:
        env.pop(name, None)
    # use the REAL subfleet for ledger calls so adoption is exercised end to end
    env["CLAUDE_LANE_SUBFLEET"] = str(SUBFLEET)
    env["SUBFLEET_STATE_DIR"] = str(state)
    # the real CLI needs the lane enrolled and a secret store that answers
    config.save(
        {"accounts": [], "enrolled": {"lane@example.com": "secret-lane"}, "codex_homes": []}
    )
    secret = tmp_path / "secret-store"
    secret.write_text('#!/bin/bash\n[ "$1" = get ] || exit 1\nprintf \'token-%s\\n\' "$2"\n')
    secret.chmod(0o755)
    env["SUBFLEET_SECRET_STORE_CMD"] = str(secret)

    owned = tmp_path / "delegate-prompt-abc.md"
    owned.write_text("merged prompt\n")
    workdir = paths["workdir"]
    run_id = run_ledger.start_run(
        family="claude",
        model="claude-fable-5",
        lane="lane@example.com",
        workdir=workdir,
        prompt=owned,
        out=paths["output"],
        caller={"session_id": SESSION},
        slug="claude-adopt",
        launcher="subfleet run",
    )
    env["SUBFLEET_RUN_ID"] = run_id
    env["SUBFLEET_RUN_OWNED_PROMPT"] = str(owned)
    env["SUBFLEET_RUN_LANE_LOG"] = str(tmp_path / "answer.lane.log")
    completed = subprocess.run(
        [
            # -a plus -A is the exact shape `subfleet run` dispatches detached:
            # start pinned, allowed to re-pick on a hard limit. It must not
            # be rejected as contradictory.
            str(CLAUDE_RUNNER), "-a", "lane@example.com", "-A", "-C", str(workdir),
            "-p", str(owned), "-o", str(paths["output"]),
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert paths["output"].read_text().strip() == "adopted answer"
    assert not owned.exists(), "the delegate-owned prompt is removed on exit"
    metas = list((state / "runs").glob("*/meta.json"))
    assert len(metas) == 1
    meta = json.loads(metas[0].read_text())
    assert meta["id"] == run_id and meta["rc"] == 0 and meta["pid"] > 0
    assert meta["lane"] == "lane@example.com" and meta["caller"]["session_id"] == SESSION


def test_wait_cli_returns_orphan_rc_for_reaped_runs(tmp_path, capsys):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    reaped = _start(tmp_path, "reaped", pid=child.pid)
    assert run_ledger.reap_orphans(grace_s=0) == [reaped]
    meta = run_ledger.load_run(reaped)[1]
    assert meta["orphaned"] and meta["finished_at"] is not None
    assert cli.main(["wait", reaped]) == 125, "a reaped run is never a success"
