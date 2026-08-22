"""Durable run-ledger recording, retention, and views."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from carpool import cli, paths, run_ledger


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ledger.invalid")
    _git(repo, "config", "user.name", "Run Ledger Test")
    (repo / "tracked.txt").write_text("before\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_record_run_copies_artifacts_and_finalizes_all_metadata(
    tmp_path, monkeypatch
):
    repo, head = _repo(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("standing preamble\n\nactual prompt\n")
    out = tmp_path / "answer.md"
    err = tmp_path / "answer.err.log"
    lane_log = tmp_path / "answer.lane.log"
    claude_dir = tmp_path / "claude"
    transcript = claude_dir / "projects" / "project" / "session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"assistant"}\n')
    monkeypatch.setenv("CARPOOL_CLAUDE_DIR", str(claude_dir))

    run_id = run_ledger.start_run(
        family="claude",
        model="claude-model",
        lane="claude-lane-a",
        workdir=repo,
        prompt=prompt,
        out=out,
        err=err,
        lane_log=lane_log,
        original_out="artifacts/Final result.md",
        decision_json={"family": "claude", "result": None, "reason": "fit"},
        started="2026-08-22T10:00:00+00:00",
    )

    assert run_id == "20260822-100000-final-result-md"
    run_dir = paths.runs_dir() / run_id
    running = json.loads((run_dir / "meta.json").read_text())
    assert running["finished_at"] is None and running["rc"] is None
    assert running["git_head_before"] == head
    assert (run_dir / "prompt.md").read_text() == prompt.read_text()
    assert _mode(paths.runs_dir()) == 0o700
    assert _mode(run_dir) == 0o700
    assert _mode(run_dir / "prompt.md") == 0o600
    assert _mode(run_dir / "meta.json") == 0o600

    out.write_text("durable answer\n")
    err.write_text("provider warning\n")
    lane_log.write_text("detached progress\n")
    _git(repo, "update-ref", "refs/claude-salvage/test-run", head)

    run_ledger.finish_run(
        run_id,
        rc=7,
        lane="claude-lane-a",
        session_id="session-1",
        finished="2026-08-22T10:00:05+00:00",
    )

    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["family"] == "claude"
    assert meta["model"] == "claude-model"
    assert meta["lane"] == "claude-lane-a"
    assert meta["workdir"] == str(repo)
    assert meta["git_head_before"] == meta["git_head_after"] == head
    assert meta["rc"] == 7
    assert meta["started_at"] == "2026-08-22T10:00:00+00:00"
    assert meta["finished_at"] == "2026-08-22T10:00:05+00:00"
    assert meta["duration_s"] == 5.0
    assert meta["original_out_path"] == "artifacts/Final result.md"
    assert meta["session_id"] == "session-1"
    assert meta["transcript_path"] == str(transcript)
    assert meta["salvage_refs"] == [
        {"ref": "refs/claude-salvage/test-run", "sha": head}
    ]
    assert meta["routing_decision"]["result"] == 7
    assert not any(key.startswith("_") for key in meta)
    assert (run_dir / "out.md").read_text() == "durable answer\n"
    assert (run_dir / "err.log").read_text() == "provider warning\n"
    assert (run_dir / "lane.log").read_text() == "detached progress\n"
    for artifact in ("meta.json", "prompt.md", "out.md", "err.log", "lane.log"):
        assert _mode(run_dir / artifact) == 0o600


def test_runs_list_load_format_and_running_marker(tmp_path):
    workdir = tmp_path / "some-worktree"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n")

    old_out, old_err = tmp_path / "old.md", tmp_path / "old.err.log"
    old_id = run_ledger.start_run(
        family="codex",
        model="codex-old",
        lane="/lanes/one",
        workdir=workdir,
        prompt=prompt,
        out=old_out,
        err=old_err,
        started="2026-08-22T08:00:00+00:00",
    )
    old_out.write_text("old output\n")
    old_err.write_text("old error\n")
    run_ledger.finish_run(
        old_id,
        rc=0,
        finished="2026-08-22T08:00:02+00:00",
    )

    new_id = run_ledger.start_run(
        family="codex",
        model="codex-new",
        lane="/lanes/two",
        workdir=workdir,
        prompt=prompt,
        out=tmp_path / "new.md",
        err=tmp_path / "new.err.log",
        started="2026-08-22T09:00:00+00:00",
    )

    assert run_ledger.list_runs(last=1) == [
        {
            "id": new_id,
            "family": "codex",
            "model": "codex-new",
            "lane": "/lanes/two",
            "rc": None,
            "status": "RUNNING",
            "out_bytes": 0,
            "duration_s": None,
            "workdir": "some-worktree",
        }
    ]
    assert run_ledger.in_flight_counts() == {("codex", "/lanes/two"): 1}

    rendered = run_ledger.format_runs(run_ledger.list_runs())
    assert new_id in rendered and "RUNNING" in rendered
    assert old_id in rendered and "old output" not in rendered

    old_dir, old_meta = run_ledger.load_run(old_id)
    assert old_meta["rc"] == 0
    assert (old_dir / "out.md").read_text() == "old output\n"
    assert (old_dir / "err.log").read_text() == "old error\n"


def test_record_run_prunes_by_count_and_bytes(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p")
    monkeypatch.setattr(run_ledger, "MAX_RUNS", 2)

    ids = []
    for hour in range(3):
        out = tmp_path / f"answer-{hour}.md"
        err = tmp_path / f"answer-{hour}.err.log"
        run_id = run_ledger.start_run(
            family="codex",
            model="codex-model",
            lane="/lane",
            workdir=workdir,
            prompt=prompt,
            out=out,
            err=err,
            started=f"2026-08-22T0{hour}:00:00+00:00",
        )
        out.write_text(str(hour))
        run_ledger.finish_run(
            run_id,
            rc=0,
            finished=f"2026-08-22T0{hour}:00:01+00:00",
        )
        ids.append(run_id)

    assert [path.name for path in run_ledger.run_directories()] == ids[1:][::-1]

    monkeypatch.setattr(
        run_ledger,
        "_directory_size",
        lambda path: 3 if path.is_dir() else 0,
    )
    removed = run_ledger.prune(max_runs=500, max_bytes=3)
    assert removed == [ids[1]]
    assert [path.name for path in run_ledger.run_directories()] == [ids[2]]


def test_failed_finish_still_creates_empty_out_and_err(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt")
    run_id = run_ledger.start_run(
        family="codex",
        model="codex-model",
        lane="/lane",
        workdir=workdir,
        prompt=prompt,
        out=tmp_path / "missing.md",
        err=tmp_path / "missing.err",
        started="2026-08-22T00:00:00+00:00",
    )

    run_ledger.finish_run(
        run_id,
        rc=9,
        finished="2026-08-22T00:00:01+00:00",
    )

    run_dir = paths.runs_dir() / run_id
    assert (run_dir / "out.md").read_bytes() == b""
    assert (run_dir / "err.log").read_bytes() == b""


def test_start_prompt_is_immutable_and_duration_keeps_fractional_precision(
    tmp_path,
):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("the prompt actually dispatched\n")
    run_id = run_ledger.start_run(
        family="codex",
        model="codex-model",
        lane="/lane",
        workdir=workdir,
        prompt=prompt,
        out=tmp_path / "out.md",
        err=tmp_path / "err.log",
        started="2026-08-22T10:00:00.900000+00:00",
    )
    prompt.write_text("later caller edit\n")

    run_ledger.finish_run(
        run_id,
        rc=0,
        finished="2026-08-22T10:00:01.100000+00:00",
    )

    run_dir = paths.runs_dir() / run_id
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["duration_s"] == 0.2
    assert not any(key.startswith("_") for key in meta)
    assert (run_dir / "prompt.md").read_text() == "the prompt actually dispatched\n"


def test_retention_prunes_abandoned_directory_and_lists_full_collision_ids(
    tmp_path, monkeypatch
):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n")
    long_out = tmp_path / ("x" * 40)
    ids = [
        run_ledger.start_run(
            family="codex",
            model="codex-model",
            lane="/lane",
            workdir=workdir,
            prompt=prompt,
            out=long_out,
            started="2026-08-22T10:00:00+00:00",
        )
        for _ in range(2)
    ]
    rendered = run_ledger.format_runs(run_ledger.list_runs())
    assert ids[0] in rendered
    assert ids[1] in rendered
    assert len(ids[1]) > 56

    abandoned = paths.runs_dir() / "20260822-090000-abandoned"
    abandoned.mkdir()
    (abandoned / "partial").write_text("crash debris")
    monkeypatch.setattr(run_ledger, "MAX_RUNS", 2)
    assert run_ledger.prune() == [abandoned.name]
    assert not abandoned.exists()


def test_runs_cli_lists_and_shows_artifacts(tmp_path, capsys):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n")
    out = tmp_path / "out.md"
    err = tmp_path / "err.log"
    run_id = run_ledger.start_run(
        family="codex",
        model="codex-model",
        lane="/lanes/one",
        workdir=workdir,
        prompt=prompt,
        out=out,
        err=err,
    )
    out.write_text("answer\n")
    err.write_text("warning\n")
    run_ledger.finish_run(run_id, rc=0)

    assert cli.main(["runs", "--last", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == run_id
    assert cli.main(["runs", "show", run_id, "--err"]) == 0
    shown = capsys.readouterr().out
    assert "--- out.md ---" in shown and "answer" in shown
    assert "--- err.log ---" in shown and "warning" in shown


def test_record_run_cli_validates_and_updates(tmp_path, capsys):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt")
    out = tmp_path / "out.md"
    assert cli.main([
        "_record-run", "--phase", "start", "--family", "codex",
        "--model", "codex-model", "--workdir", str(workdir),
        "--prompt", str(prompt), "--out", str(out),
    ]) == 0
    run_id = capsys.readouterr().out.strip()
    assert cli.main([
        "_record-run", "--phase", "update", "--run-id", run_id,
        "--lane", "/lanes/two",
    ]) == 0
    assert cli.main([
        "_record-run", "--phase", "finish", "--run-id", run_id, "--rc", "9",
    ]) == 0
    assert run_ledger.load_run(run_id)[1]["lane"] == "/lanes/two"
    assert run_ledger.load_run(run_id)[1]["rc"] == 9
