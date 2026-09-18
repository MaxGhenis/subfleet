"""Durable run-ledger recording, retention, and CLI views."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from subfleet import cli, paths, run_ledger


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
    _git(repo, "config", "user.email", "ledger@example.com")
    _git(repo, "config", "user.name", "Run Ledger")
    (repo / "tracked.txt").write_text("before\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _start_cli(capsys, *, family: str, model: str, lane: str, workdir: Path,
               prompt: Path, out: Path, err: Path, started: str,
               original_out: str | None = None, lane_log: Path | None = None,
               decision: dict | None = None) -> str:
    argv = [
        "_record-run", "--phase", "start", "--family", family,
        "--model", model, "--lane", lane, "--workdir", str(workdir),
        "--prompt", str(prompt), "--out", str(out), "--err", str(err),
        "--started", started,
    ]
    if original_out is not None:
        argv += ["--original-out", original_out]
    if lane_log is not None:
        argv += ["--lane-log", str(lane_log)]
    if decision is not None:
        argv += ["--decision-json", json.dumps(decision)]
    assert cli.main(argv) == 0
    return capsys.readouterr().out.strip()


def test_record_run_copies_artifacts_and_finalizes_all_metadata(
    tmp_path, monkeypatch, capsys
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
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(claude_dir))

    run_id = _start_cli(
        capsys,
        family="claude",
        model="claude-fable-5-1",
        lane="lane@example.com",
        workdir=repo,
        prompt=prompt,
        out=out,
        err=err,
        lane_log=lane_log,
        original_out="session scratch/My result!.md",
        decision={"family": "claude", "result": None, "reason": "voice"},
        started="2026-08-22T10:00:00+00:00",
    )

    assert run_id == "20260822-100000-my-result-md"
    run_dir = paths.runs_dir() / run_id
    running = json.loads((run_dir / "meta.json").read_text())
    assert running["finished_at"] is None and running["rc"] is None
    assert running["git_head_before"] == head
    assert (run_dir / "prompt.md").read_text() == prompt.read_text()

    out.write_text("durable answer\n")
    err.write_text("provider warning\n")
    lane_log.write_text("detached progress\n")
    _git(repo, "update-ref", "refs/claude-salvage/test-run", head)

    assert cli.main([
        "_record-run", "--phase", "finish", "--run-id", run_id,
        "--rc", "7", "--lane", "lane@example.com",
        "--session-id", "session-1",
        "--finished", "2026-08-22T10:00:05+00:00",
    ]) == 0
    assert capsys.readouterr().out == ""

    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["family"] == "claude"
    assert meta["model"] == "claude-fable-5-1"
    assert meta["lane"] == "lane@example.com"
    assert meta["workdir"] == str(repo)
    assert meta["git_head_before"] == meta["git_head_after"] == head
    assert meta["rc"] == 7
    assert meta["started_at"] == "2026-08-22T10:00:00+00:00"
    assert meta["finished_at"] == "2026-08-22T10:00:05+00:00"
    assert meta["duration_s"] == 5.0
    assert meta["original_out_path"] == "session scratch/My result!.md"
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


def test_runs_list_show_and_running_marker(tmp_path, capsys):
    workdir = tmp_path / "some-worktree"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n")

    old_out, old_err = tmp_path / "old.md", tmp_path / "old.err.log"
    old_id = _start_cli(
        capsys, family="codex", model="gpt-old", lane="/lanes/one",
        workdir=workdir, prompt=prompt, out=old_out, err=old_err,
        started="2026-08-22T08:00:00+00:00",
    )
    old_out.write_text("old output\n")
    old_err.write_text("old error\n")
    assert cli.main([
        "_record-run", "--phase", "finish", "--run-id", old_id, "--rc", "0",
        "--finished", "2026-08-22T08:00:02+00:00",
    ]) == 0
    capsys.readouterr()

    new_id = _start_cli(
        capsys, family="codex", model="gpt-new", lane="/lanes/two",
        workdir=workdir, prompt=prompt, out=tmp_path / "new.md",
        err=tmp_path / "new.err.log", started="2026-08-22T09:00:00+00:00",
    )

    assert cli.main(["runs", "--last", "1", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    expected = {
        "id": new_id,
        "family": "codex",
        "model": "gpt-new",
        "lane": "/lanes/two",
        "rc": None,
        "status": "RUNNING",
        "out_bytes": 0,
        "duration_s": None,
        "workdir": "some-worktree",
        "pid": None,
        "caller_session": None,
        "notify": None,
        "launcher": None,
        "finished_at": None,
    }
    assert {key: listed[0][key] for key in expected} == expected
    assert listed[0]["out_path"] == str(tmp_path / "new.md")

    assert cli.main(["runs"]) == 0
    assert "RUNNING" in capsys.readouterr().out
    assert cli.main(["runs", "show", old_id]) == 0
    shown = capsys.readouterr().out
    assert "--- out.md ---" in shown and "old output" in shown
    assert "old error" not in shown
    assert cli.main(["runs", "show", old_id, "--err"]) == 0
    shown = capsys.readouterr().out
    assert "old output" in shown and "--- err.log ---" in shown and "old error" in shown


def test_record_run_prunes_by_count_and_bytes(tmp_path, monkeypatch, capsys):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p")
    monkeypatch.setattr(run_ledger, "MAX_RUNS", 2)

    ids = []
    for hour in range(3):
        out = tmp_path / f"answer-{hour}.md"
        err = tmp_path / f"answer-{hour}.err.log"
        run_id = _start_cli(
            capsys, family="codex", model="gpt", lane="/lane",
            workdir=workdir, prompt=prompt, out=out, err=err,
            started=f"2026-08-22T0{hour}:00:00+00:00",
        )
        out.write_text(str(hour))
        assert cli.main([
            "_record-run", "--phase", "finish", "--run-id", run_id, "--rc", "0",
            "--finished", f"2026-08-22T0{hour}:00:01+00:00",
        ]) == 0
        capsys.readouterr()
        ids.append(run_id)

    assert [path.name for path in run_ledger.run_directories()] == ids[1:][::-1]

    monkeypatch.setattr(
        run_ledger, "_directory_size",
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
        family="codex", model="gpt", lane="/lane", workdir=workdir,
        prompt=prompt, out=tmp_path / "missing.md", err=tmp_path / "missing.err",
        started="2026-08-22T00:00:00+00:00",
    )

    run_ledger.finish_run(
        run_id, rc=9, finished="2026-08-22T00:00:01+00:00"
    )

    run_dir = paths.runs_dir() / run_id
    assert (run_dir / "out.md").read_bytes() == b""
    assert (run_dir / "err.log").read_bytes() == b""


def test_codex_resume_target_lazily_resolves_legacy_metadata(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / ".codex-3"
    rollout_dir = home / "sessions" / "2026" / "08" / "28"
    rollout_dir.mkdir(parents=True)
    thread_id = "01a03958-91a2-7682-a883-d08e638b0192"
    rollout = rollout_dir / f"rollout-test-{thread_id}.jsonl"
    rollout.write_text('{}\n')
    prompt = tmp_path / "prompt.md"
    prompt.write_text("original task\n")
    out = tmp_path / "out.md"
    out.write_text("answer\n")
    err = tmp_path / "err.log"
    err.write_text(
        f"OpenAI Codex\nsession id: {thread_id}\n"
        "tool output\nsession id: 01a030c3-164a-7583-bd2e-b7c145c3bdfa\n"
    )
    run_id = run_ledger.start_run(
        family="codex",
        model="gpt-test",
        lane=str(home),
        workdir=workdir,
        prompt=prompt,
        out=out,
        err=err,
        started="2026-08-28T10:00:00+00:00",
    )
    run_ledger.finish_run(
        run_id, rc=0, finished="2026-08-28T10:00:01+00:00"
    )

    run_dir = paths.runs_dir() / run_id
    legacy = json.loads((run_dir / "meta.json").read_text())
    for key in ("codex_thread_id", "codex_home", "rollout_path"):
        legacy.pop(key)
    (run_dir / "meta.json").write_text(json.dumps(legacy))

    target = run_ledger.codex_resume_target(run_id)
    assert target["codex_thread_id"] == thread_id
    assert target["codex_home"] == str(home)
    assert target["rollout_path"] == str(rollout)
    assert target["model"] == "gpt-test"
    assert target["workdir"] == str(workdir)
    assert target["meta"]["codex_thread_id"] == thread_id


def test_codex_thread_capture_never_scans_past_provider_header(tmp_path):
    nested = "01a030c3-164a-7583-bd2e-b7c145c3bdfa"
    err = tmp_path / "err.log"
    err.write_text(
        "OpenAI Codex\n--------\nmodel: gpt-test\n--------\n"
        f"user\ntool output\nsession id: {nested}\n"
    )

    assert run_ledger._codex_thread_id(err) is None


def test_meta_only_event_is_completed_and_never_in_flight():
    event_id = run_ledger.record_event(
        "reset",
        family="codex",
        lane="/lanes/one",
        metadata={
            "account": "lane@example.com",
            "credit_id": "credit-1",
            "response": {"code": "reset", "windows_reset": 2},
        },
        occurred="2026-08-22T10:30:00+00:00",
    )

    run_dir = paths.runs_dir() / event_id
    assert {path.name for path in run_dir.iterdir()} == {"meta.json"}
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["event"] == "reset"
    assert meta["family"] == "codex" and meta["lane"] == "/lanes/one"
    assert meta["started_at"] == meta["finished_at"]
    assert meta["event_meta"]["response"] == {"code": "reset", "windows_reset": 2}
    assert run_ledger.in_flight_counts() == {}
    assert run_ledger.list_runs(1)[0]["model"] == "reset"


def test_start_prompt_is_immutable_and_duration_keeps_fractional_precision(
    tmp_path,
):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("the prompt actually dispatched\n")
    run_id = run_ledger.start_run(
        family="codex", model="gpt", lane="/lane", workdir=workdir,
        prompt=prompt, out=tmp_path / "out.md", err=tmp_path / "err.log",
        started="2026-08-22T10:00:00.900000+00:00",
    )
    prompt.write_text("later caller edit\n")

    run_ledger.finish_run(
        run_id, rc=0, finished="2026-08-22T10:00:01.100000+00:00"
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
            family="codex", model="gpt", lane="/lane", workdir=workdir,
            prompt=prompt, out=long_out,
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


def test_output_collisions_flags_live_runs_and_names_the_last_writer(
    tmp_path, monkeypatch
):
    """2026-09-04: a killed misroute and its pinned replacement were launched
    with the same -o path; the file at that path was later attributed to the
    wrong run by three readers. The ledger must say which runs name a path and
    whether one of them is still writing it."""
    prompt = tmp_path / "p.md"
    prompt.write_text("x\n")
    out = tmp_path / "shared.md"
    first = run_ledger.start_run(
        family="claude", model="claude-opus-5", lane="a@x", workdir=tmp_path,
        prompt=prompt, out=out, original_out=str(out), pid=4242,
        started="2026-09-04T18:04:24-04:00",
    )
    second = run_ledger.start_run(
        family="claude", model="claude-opus-5", lane="b@x", workdir=tmp_path,
        prompt=prompt, out=out, original_out=str(out), pid=4343,
        started="2026-09-04T18:04:55-04:00",
    )
    out.write_text("deliverable\n")
    run_ledger.finish_run(second, rc=0)
    monkeypatch.setattr(run_ledger, "_pid_alive", lambda pid: pid == 4242)
    hits = run_ledger.output_collisions(out)
    assert [(h["id"], h["live"]) for h in hits] == [(second, False), (first, True)]
    assert hits[0]["finished_at"] is not None
    assert run_ledger.output_collisions(tmp_path / "other.md") == []
    assert run_ledger.output_collisions(None) == []


def test_running_lane_reports_transcript_idle_but_stays_running(tmp_path, monkeypatch):
    """2026-09-05: doc_019-B wrote its durable report and its session transcript
    went quiet at 01:51 while the wrapper stayed alive for two hours — but
    t1-R's transcript was quiet for 40 minutes while its scratch directory was
    busy. The row reports idle_s and renders the idle time; it never concludes
    STALE, because a kill on that conclusion would have cost seven hours."""
    import os
    import time

    claude_dir = tmp_path / "dot-claude"
    project = claude_dir / "projects" / "-some-project"
    project.mkdir(parents=True)
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(claude_dir))
    prompt = tmp_path / "p.md"
    prompt.write_text("x\n")
    run_id = run_ledger.start_run(
        family="claude", model="claude-opus-5", lane="a@x", workdir=tmp_path,
        prompt=prompt, out=tmp_path / "out.md", pid=4242,
        started="2026-09-05T01:33:22-04:00",
    )
    run_ledger.update_run(run_id, session_id="sess-quiet")
    transcript = project / "sess-quiet.jsonl"
    transcript.write_text('{"type":"assistant"}\n')
    monkeypatch.setattr(run_ledger, "_pid_alive", lambda pid: pid == 4242)

    fresh = run_ledger.list_runs(5)[0]
    assert fresh["status"] == "RUNNING" and fresh["idle_s"] is not None and fresh["idle_s"] < 60
    assert "idle" not in run_ledger.format_runs([fresh])

    old = time.time() - run_ledger.STALE_AFTER_S - 65
    os.utime(transcript, (old, old))
    quiet = run_ledger.list_runs(5)[0]
    assert quiet["status"] == "RUNNING" and quiet["idle_s"] >= run_ledger.STALE_AFTER_S
    rendered = run_ledger.format_runs([quiet])
    assert "STALE" not in rendered
    assert "RUNNING" in rendered, "the rc token pollers parse must stay RUNNING"
    assert rendered.rstrip().endswith("· transcript idle 21m")

    transcript.unlink()
    unknown = run_ledger.list_runs(5)[0]
    assert unknown["status"] == "RUNNING" and unknown["idle_s"] is None


def test_in_flight_runs_never_age_out_of_the_listing(tmp_path, monkeypatch):
    """2026-09-05: a 7-hour lane fell below `subfleet runs`' newest-20 window and
    the orchestrator's poll silently lost the one inherited run still
    outstanding. Unfinished runs are always listed; `last` bounds the finished
    rows after them."""
    prompt = tmp_path / "p.md"
    prompt.write_text("x\n")
    monkeypatch.setattr(run_ledger, "_pid_alive", lambda pid: True)
    old_running = run_ledger.start_run(
        family="claude", model="claude-opus-5", lane="a@x", workdir=tmp_path,
        prompt=prompt, out=tmp_path / "old.md", pid=100,
        started="2026-09-04T21:01:36-04:00",
    )
    finished = []
    for i in range(4):
        rid = run_ledger.start_run(
            family="claude", model="claude-opus-5", lane="b@x", workdir=tmp_path,
            prompt=prompt, out=tmp_path / f"f{i}.md", pid=200 + i,
            started=f"2026-09-05T03:0{i}:00-04:00",
        )
        run_ledger.finish_run(rid, rc=0)
        finished.append(rid)
    ids = [row["id"] for row in run_ledger.list_runs(2)]
    assert old_running in ids, ids
    assert ids == sorted(ids, reverse=True)
    assert len([i for i in ids if i in finished]) == 2
    assert [row["id"] for row in run_ledger.list_runs(2, running_only=True)] == [old_running]


def test_orphaned_runs_do_not_count_as_in_flight(tmp_path, monkeypatch):
    """2026-09-05: four ORPHANED rows (runner pid gone, never finished) counted
    as in-flight on blind Opus lanes, which BLIND_LANE_MAX_IN_FLIGHT then
    withheld from every pick; the router read that as "opus exhausted" and
    escalated standard work to Astra. A dead runner occupies nothing."""
    prompt = tmp_path / "p.md"
    prompt.write_text("x\n")
    monkeypatch.setattr(run_ledger, "_pid_alive", lambda pid: pid == 100)
    run_ledger.start_run(
        family="claude", model="claude-opus-5", lane="a@x", workdir=tmp_path,
        prompt=prompt, out=tmp_path / "alive.md", pid=100,
    )
    run_ledger.start_run(
        family="claude", model="claude-opus-5", lane="a@x", workdir=tmp_path,
        prompt=prompt, out=tmp_path / "dead.md", pid=101,
    )
    run_ledger.start_run(
        family="claude", model="claude-opus-5", lane="b@x", workdir=tmp_path,
        prompt=prompt, out=tmp_path / "unknown.md", pid=None,
    )
    counts = run_ledger.in_flight_counts()
    assert counts[("claude", "a@x")] == 1
    assert counts[("claude", "b@x")] == 1
