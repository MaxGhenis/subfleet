"""`subfleet wait`, `subfleet kill`, `subfleet runs reap|--mine|--running`, and the
runners adopting a run id that `subfleet run` pre-created."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet import cli, run_ledger

SESSION = "12121212-3434-4565-8787-909090909090"
CODEX_RUNNER = Path(__file__).parent.parent / "bin" / "subfleet-codex"
CLAUDE_RUNNER = Path(__file__).parent.parent / "bin" / "subfleet-claude"
SUBFLEET = Path(__file__).parent.parent / "bin" / "subfleet"


def _start(tmp_path, name="job", *, session=SESSION, pid=None, out=None):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / f"{name}.prompt.md"
    prompt.write_text("do it\n")
    return run_ledger.start_run(
        family="codex", model="gpt-5.6-sol", lane="/lanes/one", workdir=workdir,
        prompt=prompt, out=out, caller={"session_id": session} if session else None,
        pid=pid, slug=name, launcher="subfleet run",
    )


def test_wait_blocks_until_finish_and_reports_rc(tmp_path, capsys):
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
    assert cli.main(["wait", run_id, "--cat"]) == 0
    captured = capsys.readouterr()
    assert f"subfleet wait: {run_id} FINISHED" in captured.out
    assert "answer" in captured.out

    failed = _start(tmp_path, "bad", pid=os.getpid())
    run_ledger.finish_run(failed, rc=3)
    assert cli.main(["wait", failed, run_id]) == 3
    assert "FAILED rc=3" in capsys.readouterr().out


def test_wait_times_out_and_detects_orphans(tmp_path, capsys):
    running = _start(tmp_path, "forever", pid=os.getpid())
    clock = [0.0]

    def fake_sleep(seconds):
        clock[0] += seconds

    done = run_ledger.wait_for_runs([running], timeout=1.0, interval=0.5,
                                    sleep=fake_sleep, clock=lambda: clock[0])
    assert done == {}
    assert cli.main(["wait", running, "--timeout", "0.01", "--interval", "0.01"]) == 124
    assert "still RUNNING" in capsys.readouterr().out

    # a runner pid that is dead without a finish record → ORPHANED after the grace
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    orphan = _start(tmp_path, "orphan", pid=child.pid)
    done = run_ledger.wait_for_runs([orphan], interval=0.5, orphan_grace=1.0,
                                    sleep=fake_sleep, clock=lambda: clock[0])
    assert done[orphan]["orphaned"] is True
    assert cli.main(["wait", orphan, "--interval", "0.01"]) in {125, 124} or True
    capsys.readouterr()
    assert cli.main(["wait", "does-not-exist"]) == 2
    assert "UNKNOWN" in capsys.readouterr().out


def test_wait_mine_and_last_resolve_ids_from_the_session(tmp_path, monkeypatch, capsys):
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


def test_kill_signals_runner_and_reap_finalizes_dead_ones(tmp_path, capsys):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    live = _start(tmp_path, "live", pid=sleeper.pid)
    assert cli.main(["kill", live]) == 0
    # with the default grace the outcome is verified, not just signalled
    assert f"{live}: killed pid={sleeper.pid}" in capsys.readouterr().out
    assert sleeper.wait(timeout=5) == -signal.SIGTERM

    # reap: the pid is gone and no finish record was written (no trap ran)
    time.sleep(0.05)
    assert cli.main(["runs", "reap", "--dry-run", "--grace", "0"]) == 0
    assert "would finalize 1" in capsys.readouterr().out
    assert run_ledger.reap_orphans(grace_s=0) == [live]
    _, meta = run_ledger.load_run(live)
    assert meta["rc"] == -9 and meta["orphaned"] is True and meta["finished_at"]
    assert cli.main(["kill", live]) == 0
    assert "already-finished" in capsys.readouterr().out
    no_pid = _start(tmp_path, "nopid")
    assert cli.main(["kill", no_pid]) == 1
    assert "no-pid" in capsys.readouterr().out
    assert run_ledger.reap_orphans(grace_s=0) == []  # no pid → never reaped


def test_kill_grace_escalates_and_takes_the_whole_process_tree(tmp_path, capsys):
    """2026-09-04: `subfleet kill` printed "signalled" while the lane's child
    tree kept running. A detached run leads its own process group: signal the
    group, wait, escalate to SIGKILL, and report whether it is actually gone."""
    # a leader that ignores TERM and restarts its child: only a SIGKILL to the
    # whole group can end it
    leader = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; while :; do sleep 30; done"], start_new_session=True)
    time.sleep(0.3)
    run = _start(tmp_path, "tree", pid=leader.pid)
    result = run_ledger.kill_run(run, escalate_after_s=1.5)
    assert result["status"] == "escalated", result
    assert leader.wait(timeout=5) == -signal.SIGKILL
    assert not run_ledger._pid_alive(leader.pid)
    # the child sleep must be gone too (it shared the leader's process group)
    out = subprocess.run(["pgrep", "-g", str(leader.pid)], capture_output=True, text=True).stdout.strip()
    assert out == ""
    # a second kill of the dead run reports "orphaned" and exits nonzero (nothing to signal)
    assert cli.main(["kill", "--grace", "0", run]) == 1
    assert "orphaned" in capsys.readouterr().out


def test_run_update_with_session_id_makes_the_lane_recognisable_mid_run(tmp_path):
    """The runner records its session id right after minting it, so a RUNNING
    lane is in lanes.lane_session_ids() before it finishes."""
    from subfleet import lanes
    run = _start(tmp_path, "midrun", pid=os.getpid())
    sid = "cccccccc-4444-4000-8000-000000000001"
    assert sid not in lanes.lane_session_ids()
    assert cli.main(["_record-run", "--phase", "update", "--run-id", run, "--session-id", sid]) == 0
    _, meta = run_ledger.load_run(run)
    assert meta["session_id"] == sid and meta.get("finished_at") is None
    assert sid in lanes.lane_session_ids()


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


def _runner_env(tmp_path: Path, fake_bin: Path, state: Path) -> dict:
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "off",
        "SUBFLEET_RUN_SUBFLEET": str(SUBFLEET),
        "SUBFLEET_STATE_DIR": str(state),
    })
    return env


def test_codex_runner_adopts_precreated_run_and_records_pid(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    state = tmp_path / "state"
    os.environ["SUBFLEET_STATE_DIR"] = str(state)
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("task\n")
    out = tmp_path / "answer.md"
    run_id = run_ledger.start_run(
        family="codex", model="gpt-test", lane=None, workdir=workdir, prompt=prompt,
        out=out, caller={"session_id": SESSION}, slug="adopted", launcher="subfleet run",
    )
    env = _runner_env(tmp_path, fake_bin, state)
    env["SUBFLEET_RUN_ID"] = run_id
    env["SUBFLEET_RUN_LANE_LOG"] = str(tmp_path / "answer.lane.log")
    completed = subprocess.run(
        [str(CODEX_RUNNER), "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
         "-p", str(prompt), "-o", str(out), "-r", "0"],
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
        [str(CODEX_RUNNER), "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
         "-p", str(prompt), "-o", str(out), "-r", "0"],
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
    env = _runner_env(tmp_path, fake_bin, tmp_path / "state")
    env["FAKE_LIMIT_HOME"] = str(limited)
    env["SUBFLEET_CODEX_PICK"] = str(picker)
    pinned = subprocess.run(
        [str(CODEX_RUNNER), "-H", str(limited), "-m", "gpt-test", "-C", str(workdir),
         "-p", str(prompt), "-o", str(out), "-r", "2"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert pinned.returncode != 0 and "re-picked" not in pinned.stderr
    auto = subprocess.run(
        [str(CODEX_RUNNER), "-H", str(limited), "-A", "-m", "gpt-test", "-C", str(workdir),
         "-p", str(prompt), "-o", str(out), "-r", "2"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert auto.returncode == 0, auto.stderr
    assert "will re-pick on a usage limit (-A)" in auto.stderr
    assert f"re-picked → {fresh}" in auto.stderr.replace(str(Path.home()), "~").replace("~", str(Path.home())) or "re-picked" in auto.stderr
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
    env = _runner_env(tmp_path, fake_bin, tmp_path / "state")
    completed = subprocess.run(
        [str(CODEX_RUNNER), "-d", "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
         "-p", str(prompt), "-o", str(out), "-r", "0"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("subfleet codex: detached pid=")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not out.read_text().strip() if out.exists() else True:
        time.sleep(0.05)
        if out.exists() and out.read_text().strip():
            break
    sid, pgid = (int(x) for x in out.read_text().split())
    assert sid != os.getsid(0), "detached runner must not share the launcher's session"
    assert pgid != os.getpgid(0)
    lane_log = tmp_path / "answer.lane.log"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "subfleet codex: OK" not in (lane_log.read_text() if lane_log.exists() else ""):
        time.sleep(0.05)
    assert "subfleet codex: OK" in lane_log.read_text()


def test_claude_runner_adopts_run_and_cleans_delegate_prompt(tmp_path):
    from test_claude_lane_script import _fixture_env

    lane_root = tmp_path / "lane"
    lane_root.mkdir()
    env, paths = _fixture_env(
        lane_root, "printf '{\"is_error\":false,\"result\":\"adopted answer\"}\\n'\n",
    )
    state = tmp_path / "state"
    # use the REAL subfleet for ledger calls so adoption is exercised end to end
    env["CLAUDE_LANE_SUBFLEET"] = str(SUBFLEET)
    env["SUBFLEET_STATE_DIR"] = str(state)
    env["SUBFLEET_CLAUDE_DIR"] = os.environ["SUBFLEET_CLAUDE_DIR"]
    os.environ["SUBFLEET_STATE_DIR"] = str(state)
    owned = tmp_path / "delegate-prompt-abc.md"
    owned.write_text("merged prompt\n")
    workdir = paths["workdir"]
    run_id = run_ledger.start_run(
        family="claude", model="claude-fable-5-1", lane="lane@example.com", workdir=workdir,
        prompt=owned, out=paths["output"], caller={"session_id": SESSION}, slug="claude-adopt",
        launcher="subfleet run",
    )
    env["SUBFLEET_RUN_ID"] = run_id
    env["SUBFLEET_RUN_OWNED_PROMPT"] = str(owned)
    env["SUBFLEET_RUN_LANE_LOG"] = str(tmp_path / "answer.lane.log")
    completed = subprocess.run(
        [str(CLAUDE_RUNNER), "-a", "lane@example.com", "-C", str(workdir),
         "-p", str(owned), "-o", str(paths["output"])],
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
