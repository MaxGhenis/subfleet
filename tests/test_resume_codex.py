"""Public same-home Codex thread resumption."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from subfleet import cli, resume_codex, run_ledger


THREAD = "01a04752-a043-7ba2-8b0c-1d6e6b13c261"


def _source_run(tmp_path: Path) -> tuple[str, Path, Path]:
    home = tmp_path / "codex-home"
    workdir = tmp_path / "work"
    home.mkdir()
    workdir.mkdir()
    rollout = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "28"
        / f"rollout-2026-08-28T10-00-00-{THREAD}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    prompt = tmp_path / "source-prompt.md"
    out = tmp_path / "source-out.md"
    err = tmp_path / "source.err.log"
    prompt.write_text("do the original task\n")
    out.write_text("partial result\n")
    err.write_text(f"Codex CLI\nsession id: {THREAD}\n")
    run_id = run_ledger.start_run(
        family="codex",
        model="gpt-5.6-sol",
        lane=str(home),
        workdir=workdir,
        prompt=prompt,
        out=out,
        err=err,
        decision_json={"cmd": ["subfleet-codex", "-s", "read-only", "-e", "ultra"]},
        started="2026-08-28T10:00:00+00:00",
    )
    run_ledger.finish_run(
        run_id,
        rc=1,
        finished="2026-08-28T10:00:01+00:00",
    )
    return run_id, home, workdir


def test_resume_dispatches_same_thread_and_records_parent(tmp_path, capsys):
    source_run_id, home, workdir = _source_run(tmp_path)
    seen: dict = {}

    def fake_runner(command, **kwargs):
        seen.update({"command": command, **kwargs})
        resumed_run_id = kwargs["env"]["SUBFLEET_RUN_ID"]
        run_paths = run_ledger.run_paths(resumed_run_id)
        Path(run_paths["out"]).write_text("continued answer\n")
        Path(run_paths["err"]).write_text(f"session id: {THREAD}\n")
        run_ledger.finish_run(resumed_run_id, rc=0, lane=str(home))
        return subprocess.CompletedProcess(command, 0, "runner ok\n", "")

    result = resume_codex.run(
        source_run_id,
        prompt="finish it carefully",
        runner=fake_runner,
        capacity_report=lambda: {
            "accounts": [{"family": "codex", "home": str(home), "status": "ok"}]
        },
    )

    assert result == 0
    assert capsys.readouterr().out == "continued answer\n"
    command = seen["command"]
    assert command[command.index("-H") + 1] == str(home)
    assert command[command.index("-T") + 1] == THREAD
    assert command[command.index("-C") + 1] == str(workdir)
    assert command[command.index("-s") + 1] == "read-only"
    assert command[command.index("-e") + 1] == "ultra"
    assert "-A" not in command
    resumed = [
        json.loads((directory / "meta.json").read_text())
        for directory in run_ledger.run_directories()
        if directory.name != source_run_id
    ][0]
    assert resumed["resumed_from"] == source_run_id
    assert resumed["codex_home"] == str(home)
    assert resumed["codex_thread_id"] == THREAD


def test_resume_parks_a_limited_original_home(tmp_path, capsys):
    source_run_id, home, _workdir = _source_run(tmp_path)
    called = False

    def forbidden_runner(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("runner should not launch")

    result = resume_codex.run(
        source_run_id,
        runner=forbidden_runner,
        capacity_report=lambda: {
            "accounts": [
                {
                    "family": "codex",
                    "home": str(home),
                    "status": "limited",
                    "limited_until": "2026-08-28T12:00:00+00:00",
                }
            ]
        },
    )

    assert result == 3
    assert called is False
    error = capsys.readouterr().err
    assert "parked" in error and "cannot migrate" in error
    assert "2026-08-28T12:00:00+00:00" in error


def test_chained_resume_preserves_effective_sandbox_and_effort(tmp_path, capsys):
    source_run_id, home, _workdir = _source_run(tmp_path)
    commands = []
    resumed_ids = []

    def fake_runner(command, **kwargs):
        commands.append(command)
        resumed_run_id = kwargs["env"]["SUBFLEET_RUN_ID"]
        resumed_ids.append(resumed_run_id)
        run_paths = run_ledger.run_paths(resumed_run_id)
        Path(run_paths["out"]).write_text("continued\n")
        Path(run_paths["err"]).write_text(f"session id: {THREAD}\n")
        run_ledger.finish_run(resumed_run_id, rc=0, lane=str(home))
        return subprocess.CompletedProcess(command, 0, "", "")

    available = lambda: {
        "accounts": [{"family": "codex", "home": str(home), "status": "ok"}]
    }
    assert resume_codex.run(
        source_run_id,
        runner=fake_runner,
        capacity_report=available,
    ) == 0
    assert resume_codex.run(
        resumed_ids[0],
        runner=fake_runner,
        capacity_report=available,
    ) == 0
    capsys.readouterr()

    for command in commands:
        assert command[command.index("-s") + 1] == "read-only"
        assert command[command.index("-e") + 1] == "ultra"
    _run_dir, second_meta = run_ledger.load_run(resumed_ids[1])
    assert second_meta["resumed_from"] == resumed_ids[0]
    assert second_meta["routing_decision"]["kind"] == "codex-resume"
    assert second_meta["routing_decision"]["result"] == 0


def test_resume_rejects_non_codex_run(tmp_path, capsys):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello\n")
    run_id = run_ledger.start_run(
        family="claude",
        model="claude-opus-5",
        lane="lane@example.com",
        workdir=tmp_path,
        prompt=prompt,
        out=None,
    )
    run_ledger.finish_run(run_id, rc=0)

    assert resume_codex.run(run_id, capacity_report=lambda: {}) == 2
    assert "not a Codex run" in capsys.readouterr().err


def test_resume_codex_cli_routes_without_falling_back_to_status(monkeypatch):
    seen = {}

    def fake_run(run_id, *, prompt, output):
        seen.update({"run_id": run_id, "prompt": prompt, "output": output})
        return 17

    monkeypatch.setattr(resume_codex, "run", fake_run)

    assert cli.main(["resume-codex", "run-123", "continue now", "-o", "answer.md"]) == 17
    assert seen == {
        "run_id": "run-123",
        "prompt": "continue now",
        "output": "answer.md",
    }
